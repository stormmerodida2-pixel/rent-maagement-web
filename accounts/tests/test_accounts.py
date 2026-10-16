from django.conf import settings
import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from notifications.models import SmsMessage
from payments.tests import make_account
from rentals.tests import make_lease

from ..models import Profile
from ..utils import is_verified, make_token


def link_in(message):
    return re.search(r'https?://\S+', message.body).group(0)


class SignupTests(TestCase):
    def setUp(self):
        cache.clear()

    def signup(self, **overrides):
        data = {
            'first_name': 'Grace', 'contact': 'grace@example.com',
            'password1': 'a-strong-pass-123', 'password2': 'a-strong-pass-123', 'accept_terms': 'on', **overrides,
        }
        return self.client.post(reverse('accounts:signup'), data)

    def test_signup_logs_in_and_sends_verification(self):
        resp = self.signup()
        self.assertRedirects(resp, reverse('accounts:welcome'))
        user = get_user_model().objects.get(email='grace@example.com')
        self.assertTrue(user.username.startswith('user-'))  # made up behind the scenes, never shown
        self.assertFalse(user.is_staff)  # landlords never need the admin
        self.assertFalse(is_verified(user))
        self.assertEqual(mail.outbox[0].to, ['grace@example.com'])
        page = self.client.get(reverse('accounts:welcome'))
        self.assertContains(page, 'Add your first property')
        self.assertContains(page, 'Please confirm your email')

    def test_terms_must_be_accepted_and_acceptance_is_recorded(self):
        resp = self.signup(accept_terms='')
        self.assertContains(resp, 'Please read and accept the terms')
        self.assertFalse(get_user_model().objects.filter(email='grace@example.com').exists())
        self.signup()
        profile = get_user_model().objects.get(email='grace@example.com').profile
        self.assertIsNotNone(profile.terms_accepted_at)
        self.assertEqual(profile.terms_version, settings.LEGAL['TERMS_VERSION'])

    def test_duplicate_email_rejected(self):
        get_user_model().objects.create_user('x', email='grace@example.com')
        resp = self.signup(contact='GRACE@example.com')
        self.assertContains(resp, 'already exists')

    def test_weak_password_rejected(self):
        resp = self.signup(password1='123', password2='123')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(email='grace@example.com').exists())

    def test_signups_are_rate_limited_per_ip(self):
        for i in range(5):
            self.client.logout()
            self.signup(contact=f'u{i}@example.com')
        self.client.logout()
        resp = self.signup(contact='u9@example.com')
        self.assertEqual(resp.status_code, 429)
        self.assertFalse(get_user_model().objects.filter(email='u9@example.com').exists())

    def test_sign_up_with_a_phone_number(self):
        resp = self.signup(contact='0712 345 678')
        self.assertRedirects(resp, reverse('accounts:verify_phone'))
        user = get_user_model().objects.get(profile__phone='254712345678')
        self.assertEqual(user.email, '')
        sms = SmsMessage.objects.get(phone='254712345678')
        code = re.search(r'\b\d{6}\b', sms.body).group(0)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), 'confirm your phone number')
        self.assertContains(self.client.post(reverse('accounts:verify_phone'), {'code': '000000' if code != '000000' else '111111'}),
                            'That code isn')
        self.client.post(reverse('accounts:verify_phone'), {'code': code})
        self.assertTrue(is_verified(user))
        self.client.logout()
        self.assertTrue(self.client.login(username='+254 712 345 678', password='a-strong-pass-123'))

    def test_phone_already_used_and_bad_input(self):
        self.signup(contact='0712345678')
        self.client.logout()
        self.assertContains(self.signup(contact='+254712345678'), 'already exists')
        self.assertContains(self.signup(contact='grace'), 'Enter an email address, or a Kenyan mobile number')


@override_settings(SITE_URL='https://rent.example')
class VerifyEmailTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client.post(reverse('accounts:signup'), {
            'first_name': 'Grace', 'contact': 'grace@example.com',
            'password1': 'a-strong-pass-123', 'password2': 'a-strong-pass-123', 'accept_terms': 'on',
        })
        self.user = get_user_model().objects.get(email='grace@example.com')

    def test_link_from_email_verifies(self):
        url = link_in(mail.outbox[0])
        self.assertTrue(url.startswith('https://rent.example/verify-email/'))
        self.client.get(url.removeprefix('https://rent.example'))
        self.assertTrue(is_verified(self.user))
        self.assertNotContains(self.client.get(reverse('rentals:dashboard')), 'Please confirm your email')

    def test_tampered_token_and_changed_email_are_rejected(self):
        token = make_token(self.user)
        self.client.get(reverse('accounts:verify_email', args=[token + 'x']))
        self.user.email = 'new@example.com'
        self.user.save()
        self.client.get(reverse('accounts:verify_email', args=[token]))
        self.assertFalse(is_verified(self.user))

    def test_resend_is_limited(self):
        url = reverse('accounts:resend_verification')
        for _ in range(5):
            self.client.post(url)
        self.assertEqual(len(mail.outbox), 1 + 3)  # signup email + 3 resends


class EmailOrPhoneLoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user('user-x1', email='grace@example.com', password='right-password-1')
        Profile.objects.create(user=self.user, phone='254712345678')

    def test_log_in_with_email_or_phone(self):
        for login_as in ('grace@example.com', 'GRACE@example.com', '0712345678', '0712 345 678', '+254712345678'):
            resp = self.client.post(reverse('login'), {'username': login_as, 'password': 'right-password-1'})
            self.assertRedirects(resp, reverse('rentals:dashboard'), msg_prefix=login_as)
            self.client.logout()
        self.assertContains(self.client.get(reverse('login')), 'Email or phone number')
        resp = self.client.post(reverse('login'), {'username': '0712345678', 'password': 'wrong'})
        self.assertContains(resp, "didn't match")

    def test_failures_count_however_the_number_is_typed(self):
        for typed in ('0712345678', '+254712345678', '254712345678', '0712 345 678', '0712-345-678'):
            self.client.post(reverse('login'), {'username': typed, 'password': 'wrong'})
        resp = self.client.post(reverse('login'), {'username': '0712345678', 'password': 'right-password-1'})
        self.assertEqual(resp.status_code, 429)


class LoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        get_user_model().objects.create_user('grace', password='right-password-1')

    def test_repeated_failures_lock_the_username(self):
        for _ in range(5):
            self.client.post(reverse('login'), {'username': 'grace', 'password': 'wrong'})
        resp = self.client.post(reverse('login'), {'username': 'grace', 'password': 'right-password-1'})
        self.assertEqual(resp.status_code, 429)

    def test_success_resets_failures(self):
        for _ in range(4):
            self.client.post(reverse('login'), {'username': 'grace', 'password': 'wrong'})
        self.client.post(reverse('login'), {'username': 'grace', 'password': 'right-password-1'})
        self.client.logout()
        for _ in range(4):
            self.client.post(reverse('login'), {'username': 'grace', 'password': 'wrong'})
        resp = self.client.post(reverse('login'), {'username': 'grace', 'password': 'right-password-1'})
        self.assertRedirects(resp, reverse('rentals:dashboard'))


@override_settings(SITE_URL='https://rent.example')
class PasswordResetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user('grace', email='grace@example.com', password='old-pass-123x')

    def test_full_reset_flow(self):
        resp = self.client.post(reverse('password_reset'), {'email': 'grace@example.com'})
        self.assertRedirects(resp, reverse('password_reset_done'))
        url = link_in(mail.outbox[0]).removeprefix('https://rent.example')
        resp = self.client.get(url, follow=True)  # Django swaps the token into the session
        self.assertContains(resp, 'Choose a new password')
        resp = self.client.post(resp.redirect_chain[-1][0], {
            'new_password1': 'brand-new-pass-456', 'new_password2': 'brand-new-pass-456',
        })
        self.assertRedirects(resp, reverse('password_reset_complete'))
        self.assertTrue(self.client.login(username='grace', password='brand-new-pass-456'))

    def test_unknown_email_gives_same_response(self):
        resp = self.client.post(reverse('password_reset'), {'email': 'nobody@example.com'})
        self.assertRedirects(resp, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 0)

    def test_reset_by_phone_with_an_sms_code(self):
        Profile.objects.create(user=self.user, phone='254712345678')
        resp = self.client.post(reverse('password_reset'), {'email': '0712 345 678'})
        self.assertRedirects(resp, reverse('password_reset_phone'))
        code = re.search(r'\b\d{6}\b', SmsMessage.objects.get(phone='254712345678').body).group(0)
        new = {'new_password1': 'brand-new-pass-456', 'new_password2': 'brand-new-pass-456'}
        wrong = '000000' if code != '000000' else '111111'
        self.assertContains(self.client.post(reverse('password_reset_phone'), {'code': wrong, **new}), 'That code isn')
        resp = self.client.post(reverse('password_reset_phone'), {'code': code, **new})
        self.assertRedirects(resp, reverse('login'))
        self.assertTrue(self.client.login(username='0712345678', password='brand-new-pass-456'))

    def test_unknown_phone_gives_same_response(self):
        resp = self.client.post(reverse('password_reset'), {'email': '0799 999 999'})
        self.assertRedirects(resp, reverse('password_reset_phone'))
        self.assertFalse(SmsMessage.objects.exists())
        resp = self.client.post(reverse('password_reset_phone'), {'code': '123456', 'new_password1': 'brand-new-pass-456',
                                                                  'new_password2': 'brand-new-pass-456'})
        self.assertContains(resp, 'That code isn')

    def test_reset_requests_are_rate_limited(self):
        for _ in range(5):
            self.client.post(reverse('password_reset'), {'email': 'grace@example.com'})
        self.assertEqual(self.client.post(reverse('password_reset'), {'email': 'grace@example.com'}).status_code, 429)


class WelcomeTests(TestCase):
    def test_checklist_progress(self):
        user = get_user_model().objects.create_user('landlord')
        self.client.force_login(user)
        resp = self.client.get(reverse('accounts:welcome'))
        self.assertEqual(resp.context['done_count'], 0)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), 'Finish setting up')

        make_lease(user)
        resp = self.client.get(reverse('accounts:welcome'))
        self.assertEqual(resp.context['done_count'], 3)
        self.assertEqual(resp.context['current']['title'], 'Add your Paybill or Till')

        account = make_account(user)
        account.verified_at = account.created_at
        account.save()
        resp = self.client.get(reverse('accounts:welcome'))
        self.assertIsNone(resp.context['current'])
        self.assertNotContains(self.client.get(reverse('rentals:dashboard')), 'Finish setting up')


class AdminLoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        get_user_model().objects.create_superuser('root', password='right-password-1')

    def test_admin_login_uses_the_app_login_and_its_limits(self):
        # The admin's own login page hands over to the app's login (throttled, with two-step login).
        self.assertRedirects(self.client.post('/admin/login/?next=/admin/', {'username': 'root', 'password': 'x'}),
                             '/login/?next=/admin/', fetch_redirect_response=False)
        for _ in range(5):
            self.client.post(reverse('login'), {'username': 'root', 'password': 'wrong'})
        self.assertEqual(self.client.post(reverse('login'), {'username': 'root', 'password': 'right-password-1'}).status_code, 429)

    def test_admin_login_still_works(self):
        resp = self.client.post(reverse('login'), {'username': 'root', 'password': 'right-password-1', 'next': '/admin/'})
        self.assertRedirects(resp, '/admin/', fetch_redirect_response=False)
