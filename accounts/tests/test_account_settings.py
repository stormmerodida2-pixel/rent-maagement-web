"""Editing your profile and changing your password from Settings."""

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEntry
from notifications.models import LandlordPreferences

from ..models import Profile
from ..utils import is_verified, mark_verified


class ProfileTests(TestCase):
    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.user = User.objects.create_user('grace', email='grace@example.com', password='old-pass-123',
                                             first_name='Grace')
        mark_verified(self.user)
        User.objects.create_user('other', email='taken@example.com')
        self.client.force_login(self.user)
        self.url = reverse('accounts:profile')

    def post(self, **data):
        return self.client.post(self.url, {'first_name': 'Grace', 'email': 'grace@example.com', 'phone': '',
                                           'current_password': '', **data})

    def test_settings_links_to_profile_and_password(self):
        page = self.client.get(reverse('accounts:settings'))
        self.assertContains(page, reverse('accounts:profile'))
        self.assertContains(page, reverse('accounts:password_change'))

    def test_change_name_without_password(self):
        resp = self.post(first_name='Grace Wanjiru')
        self.assertRedirects(resp, reverse('accounts:settings'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Grace Wanjiru')
        self.assertTrue(AuditEntry.objects.filter(action='account.profile_changed').exists())
        self.assertEqual(mail.outbox, [])  # no email change, no emails

    def test_adding_a_phone_needs_the_password_then_a_code(self):
        self.assertContains(self.post(phone='0712 345 678'), 'Enter your current password')
        resp = self.post(phone='0712 345 678', current_password='old-pass-123')
        self.assertRedirects(resp, reverse('accounts:verify_phone'))
        self.assertEqual(LandlordPreferences.for_user(self.user).phone, '254712345678')
        self.assertEqual(Profile.objects.get(user=self.user).phone, '254712345678')
        self.assertTrue(self.client.login(username='0712345678', password='old-pass-123'))  # can log in with it

    def test_changing_email_needs_current_password(self):
        resp = self.post(email='new@example.com', current_password='wrong')
        self.assertContains(resp, 'Enter your current password to change your email or phone.')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'grace@example.com')

    def test_changing_email_reconfirms_and_warns_the_old_address(self):
        resp = self.post(email='New@Example.com', current_password='old-pass-123')
        self.assertRedirects(resp, reverse('accounts:settings'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'new@example.com')
        self.assertFalse(is_verified(self.user))
        recipients = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(recipients, ['grace@example.com', 'new@example.com'])  # warning + confirmation link
        audit = AuditEntry.objects.get(action='account.profile_changed')
        self.assertNotIn('Current password', audit.changes)

    def test_email_already_used(self):
        resp = self.post(email='taken@example.com', current_password='old-pass-123')
        self.assertContains(resp, 'An account with this email already exists.')

    def test_bad_phone_and_sms_summary_needs_a_phone(self):
        self.assertContains(self.post(phone='12'), 'phone', status_code=200)
        prefs = LandlordPreferences.for_user(self.user)
        prefs.phone, prefs.report_sms = '254712345678', True
        prefs.save()
        self.assertContains(self.post(phone=''), 'Turn it off under Notifications first')

    def test_keep_an_email_or_a_phone(self):
        resp = self.post(email='', current_password='old-pass-123')
        self.assertContains(resp, 'Keep an email address or a phone number')

    def test_login_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)


class PasswordTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user('grace', email='grace@example.com', password='old-pass-123')
        self.url = reverse('accounts:password_change')

    def test_change_password_keeps_this_device_signed_in_and_notifies(self):
        self.client.login(username='grace', password='old-pass-123')
        other_device = self.client_class()
        other_device.login(username='grace', password='old-pass-123')
        resp = self.client.post(self.url, {'old_password': 'old-pass-123', 'new_password1': 'Fresh-Pass-2026',
                                           'new_password2': 'Fresh-Pass-2026'})
        self.assertRedirects(resp, reverse('accounts:settings'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Fresh-Pass-2026'))
        self.assertEqual(self.client.get(reverse('accounts:settings')).status_code, 200)  # still signed in here
        self.assertEqual(other_device.get(reverse('accounts:settings')).status_code, 302)  # signed out elsewhere
        self.assertEqual(mail.outbox[0].to, ['grace@example.com'])
        self.assertIn('password was changed', mail.outbox[0].subject)
        self.assertTrue(AuditEntry.objects.filter(action='account.password_changed').exists())

    def test_wrong_current_password(self):
        self.client.login(username='grace', password='old-pass-123')
        resp = self.client.post(self.url, {'old_password': 'nope', 'new_password1': 'Fresh-Pass-2026',
                                           'new_password2': 'Fresh-Pass-2026'})
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('old-pass-123'))

    def test_guessing_is_rate_limited(self):
        self.client.login(username='grace', password='old-pass-123')
        for _ in range(10):
            self.client.post(self.url, {'old_password': 'nope', 'new_password1': 'x', 'new_password2': 'x'})
        resp = self.client.post(self.url, {'old_password': 'old-pass-123', 'new_password1': 'Fresh-Pass-2026',
                                           'new_password2': 'Fresh-Pass-2026'}, follow=True)
        self.assertContains(resp, 'Too many attempts')
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('old-pass-123'))
