import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from payments.tests import make_account
from rentals.tests import make_lease

from .verification import is_verified, make_token


def link_in(message):
    return re.search(r'https?://\S+', message.body).group(0)


class SignupTests(TestCase):
    def setUp(self):
        cache.clear()

    def signup(self, **overrides):
        data = {
            'first_name': 'Grace', 'email': 'grace@example.com', 'username': 'grace',
            'password1': 'a-strong-pass-123', 'password2': 'a-strong-pass-123', **overrides,
        }
        return self.client.post(reverse('accounts:signup'), data)

    def test_signup_logs_in_and_sends_verification(self):
        resp = self.signup()
        self.assertRedirects(resp, reverse('accounts:welcome'))
        user = get_user_model().objects.get(username='grace')
        self.assertFalse(user.is_staff)  # landlords never need the admin
        self.assertFalse(is_verified(user))
        self.assertEqual(mail.outbox[0].to, ['grace@example.com'])
        page = self.client.get(reverse('accounts:welcome'))
        self.assertContains(page, 'Add your first property')
        self.assertContains(page, 'Please confirm your email')

    def test_duplicate_email_rejected(self):
        get_user_model().objects.create_user('x', email='grace@example.com')
        resp = self.signup(email='GRACE@example.com')
        self.assertContains(resp, 'already exists')

    def test_weak_password_rejected(self):
        resp = self.signup(password1='123', password2='123')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(username='grace').exists())

    def test_signups_are_rate_limited_per_ip(self):
        for i in range(5):
            self.client.logout()
            self.signup(username=f'u{i}', email=f'u{i}@example.com')
        self.client.logout()
        resp = self.signup(username='u9', email='u9@example.com')
        self.assertEqual(resp.status_code, 429)
        self.assertFalse(get_user_model().objects.filter(username='u9').exists())


@override_settings(SITE_URL='https://rent.example')
class VerifyEmailTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client.post(reverse('accounts:signup'), {
            'first_name': 'Grace', 'email': 'grace@example.com', 'username': 'grace',
            'password1': 'a-strong-pass-123', 'password2': 'a-strong-pass-123',
        })
        self.user = get_user_model().objects.get(username='grace')

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
        self.assertEqual(resp.context['current']['title'], 'Connect M-Pesa')

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

    def test_admin_login_is_limited_and_shares_the_counter(self):
        for _ in range(5):
            self.client.post('/admin/login/', {'username': 'root', 'password': 'wrong'})
        self.assertEqual(self.client.post('/admin/login/', {'username': 'root', 'password': 'right-password-1'}).status_code, 429)
        self.assertEqual(self.client.post(reverse('login'), {'username': 'root', 'password': 'right-password-1'}).status_code, 429)

    def test_admin_login_still_works(self):
        resp = self.client.post('/admin/login/?next=/admin/', {'username': 'root', 'password': 'right-password-1'})
        self.assertRedirects(resp, '/admin/', fetch_redirect_response=False)
