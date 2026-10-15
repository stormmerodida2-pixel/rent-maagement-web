"""Two-step login: setting it up, logging in with a code, recovery codes, trusted devices, turning it off."""

import io
import time

import pyotp
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEntry

from ..models import TwoFactor
from ..utils import TRUST_COOKIE, check_totp


class TwoFactorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user('grace', email='grace@example.com', password='Secret-pass-2026')

    def turn_on(self):
        """Set it up through the pages, as a landlord would. Returns the TOTP and the recovery codes."""
        self.client.force_login(self.user)
        url = reverse('accounts:two_factor')
        self.client.post(url, {'action': 'start'})
        page = self.client.get(url)
        self.assertContains(page, '<svg')  # the QR code
        totp = pyotp.TOTP(TwoFactor.objects.get(user=self.user).secret)
        done = self.client.post(url, {'action': 'confirm', 'code': totp.now()})
        self.client.logout()
        return totp, done.context['new_codes']

    def password_step(self, next_url=''):
        return self.client.post(reverse('login') + (f'?next={next_url}' if next_url else ''),
                                {'username': 'grace', 'password': 'Secret-pass-2026', 'next': next_url})

    def test_set_up_shows_recovery_codes_and_notifies(self):
        totp, codes = self.turn_on()
        self.assertTrue(TwoFactor.objects.get(user=self.user).enabled)
        self.assertEqual(len(codes), 10)
        self.assertIn('two-step login is on', mail.outbox[0].subject)
        self.assertTrue(AuditEntry.objects.filter(action='account.two_factor_on').exists())

    def test_wrong_first_code_keeps_it_off(self):
        self.client.force_login(self.user)
        self.client.post(reverse('accounts:two_factor'), {'action': 'start'})
        resp = self.client.post(reverse('accounts:two_factor'), {'action': 'confirm', 'code': '000000'})
        self.assertContains(resp, "That code didn")
        self.assertFalse(TwoFactor.objects.get(user=self.user).enabled)

    def test_password_alone_is_not_enough(self):
        totp, _ = self.turn_on()
        resp = self.password_step('/reports/')
        self.assertRedirects(resp, reverse('accounts:login_verify'))
        self.assertNotIn('_auth_user_id', self.client.session)  # not logged in yet
        self.assertRedirects(self.client.get(reverse('rentals:dashboard')), '/login/?next=/dashboard/',
                             fetch_redirect_response=False)
        # The setup code was just used, so the next one (a moment later) is what logs in.
        resp = self.client.post(reverse('accounts:login_verify'), {'code': totp.at(time.time() + 30)})
        self.assertRedirects(resp, '/reports/', fetch_redirect_response=False)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.user.pk)

    def test_a_code_works_only_once(self):
        two_factor_user, _ = self.turn_on()
        two_factor = TwoFactor.objects.get(user=self.user)
        now = time.time() + 120
        code = two_factor_user.at(now)
        self.assertTrue(check_totp(two_factor, code, now=now))
        self.assertFalse(check_totp(two_factor, code, now=now))  # replayed

    def test_recovery_code_works_once(self):
        _, codes = self.turn_on()
        self.password_step()
        resp = self.client.post(reverse('accounts:login_verify'), {'code': codes[0].upper()}, follow=True)
        self.assertContains(resp, 'You used a recovery code; 9 left')
        self.client.logout()
        self.password_step()
        resp = self.client.post(reverse('accounts:login_verify'), {'code': codes[0]})
        self.assertContains(resp, "That code didn")

    def test_too_many_wrong_codes_starts_over(self):
        self.turn_on()
        self.password_step()
        for _ in range(5):
            resp = self.client.post(reverse('accounts:login_verify'), {'code': '000000'})
        self.assertRedirects(resp, reverse('login'))
        self.assertRedirects(self.client.get(reverse('accounts:login_verify')), reverse('login'))

    def test_trusted_device_skips_the_code_until_revoked(self):
        totp, codes = self.turn_on()
        self.password_step()
        resp = self.client.post(reverse('accounts:login_verify'), {'code': totp.at(time.time() + 30), 'trust': '1'})
        self.assertIn(TRUST_COOKIE, resp.cookies)
        self.client.post(reverse('logout'))  # a real logout keeps the browser's other cookies
        self.assertRedirects(self.password_step(), reverse('rentals:dashboard'), fetch_redirect_response=False)
        # Turning it off and on again forgets trusted devices.
        TwoFactor.objects.filter(user=self.user).update(trust_version=99)
        self.client.post(reverse('logout'))
        self.assertRedirects(self.password_step(), reverse('accounts:login_verify'))

    def test_correct_passwords_with_codes_pending_do_not_lock_the_account(self):
        self.turn_on()
        for _ in range(8):
            self.assertRedirects(self.password_step(), reverse('accounts:login_verify'))

    def test_turning_off_needs_password_and_code(self):
        totp, _ = self.turn_on()
        self.client.force_login(self.user)
        url = reverse('accounts:two_factor')
        resp = self.client.post(url, {'action': 'disable', 'password': 'wrong', 'code': totp.now()})
        self.assertContains(resp, "That password isn")
        self.client.post(url, {'action': 'disable', 'password': 'Secret-pass-2026',
                               'code': totp.at(time.time() + 30)})
        self.assertFalse(TwoFactor.objects.filter(user=self.user).exists())
        self.assertIn('turned off', mail.outbox[-1].subject)

    def test_admin_login_goes_through_the_app_login(self):
        resp = self.client.get('/admin/login/?next=/admin/')
        self.assertRedirects(resp, '/login/?next=/admin/', fetch_redirect_response=False)

    def test_settings_shows_the_state(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('accounts:settings')), 'Two-step login')

    def test_emergency_command(self):
        self.turn_on()
        call_command('disable_two_step', 'grace', stdout=io.StringIO())
        self.assertFalse(TwoFactor.objects.filter(user=self.user).exists())

    def test_staff_can_use_it_too(self):
        from ..models import StaffMember

        owner = get_user_model().objects.create_user('landlord')
        StaffMember.objects.create(owner=owner, user=self.user)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('staff:two_factor')), 'Set up two-step login')
