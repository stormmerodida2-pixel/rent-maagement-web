"""Superadmins view the app as a landlord, for support: safeguards, and a private log the landlord never sees."""

import time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEntry
from payments.models import Payment
from rentals.models import Property
from rentals.tests import make_lease

from ..impersonation import LIMIT, SESSION_KEY
from ..models import StaffMember, SupportSession


class ImpersonationTests(TestCase):
    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.admin = User.objects.create_superuser('boss', password='pw', first_name='Stormer')
        self.landlord = User.objects.create_user('landlord', first_name='Grace', last_name='Wanjiru')
        self.lease = make_lease(self.landlord)
        self.client.force_login(self.admin)

    def start(self, mode='view', reason='Cannot upload M-Pesa statement', who=None):
        return self.client.post(reverse('impersonate_start', args=[(who or self.landlord).pk]),
                                {'reason': reason, 'mode': mode})

    def test_see_exactly_what_the_landlord_sees(self):
        page = self.client.get(reverse('impersonate'))
        self.assertContains(page, 'Grace Wanjiru')
        self.assertRedirects(self.start(), reverse('rentals:dashboard'), fetch_redirect_response=False)
        dash = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(dash, "you're viewing <strong>Grace Wanjiru</strong>")
        self.assertContains(dash, 'view only')
        self.assertContains(self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk])), str(self.lease.tenant))
        # Not told: nothing in their Activity. The private log has it.
        self.assertFalse(AuditEntry.objects.filter(owner=self.landlord, action__startswith='support').exists())
        activity = self.client.get(reverse('audit:activity'))
        self.assertNotContains(activity, 'Rentipay support')
        self.assertNotContains(activity, 'opened your account')
        log = SupportSession.objects.get()
        self.assertEqual((log.superadmin, log.landlord, log.reason, log.allow_changes, log.ended_at),
                         (self.admin, self.landlord, 'Cannot upload M-Pesa statement', False, None))

    def test_view_only_changes_nothing(self):
        self.start()
        resp = self.client.post(reverse('rentals:property_create'), {'name': 'Sneaky'}, follow=True)
        self.assertContains(resp, 'view only, so nothing was changed')
        self.assertFalse(Property.objects.filter(name='Sneaky').exists())

    def test_changes_allowed_are_logged_under_the_superadmin(self):
        self.start(mode='changes')
        payment = Payment.objects.create(lease=self.lease, amount=Decimal('100'), method='cash')
        self.client.post(reverse('payments:payment_void', args=[payment.pk]), {'reason': 'Duplicate'})
        entry = AuditEntry.objects.filter(action__contains='void').latest('created_at')
        self.assertEqual((entry.owner, entry.actor), (self.landlord, self.admin))
        self.assertEqual(entry.actor_label, 'Rentipay support')  # not the superadmin's own name

    def test_their_password_and_staff_area_stay_off_limits(self):
        self.start(mode='changes')
        for url in (reverse('accounts:password_change'), reverse('accounts:two_factor')):
            self.assertRedirects(self.client.get(url), reverse('rentals:dashboard'), fetch_redirect_response=False)

    def test_stop_returns_and_is_logged(self):
        self.start()
        resp = self.client.post(reverse('impersonate_stop'))
        self.assertRedirects(resp, reverse('impersonate'))
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertIsNotNone(SupportSession.objects.get().ended_at)
        self.assertFalse(AuditEntry.objects.filter(owner=self.landlord, action__startswith='support').exists())
        self.assertNotContains(self.client.get(reverse('rentals:dashboard')), "you're viewing")

    def test_ends_after_an_hour(self):
        self.start()
        session = self.client.session
        session[SESSION_KEY]['started'] = time.time() - LIMIT - 1
        session.save()
        page = self.client.get(reverse('rentals:dashboard'), follow=True)
        self.assertNotContains(page, "you're viewing")
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertIsNotNone(SupportSession.objects.get().ended_at)

    def test_reason_required(self):
        self.start(reason='  ')
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_only_superadmins_and_only_landlords(self):
        other_admin = get_user_model().objects.create_superuser('boss2', password='pw')
        self.assertEqual(self.start(who=other_admin).status_code, 404)
        caretaker = get_user_model().objects.create_user('peter')
        StaffMember.objects.create(owner=self.landlord, user=caretaker)
        self.assertEqual(self.start(who=caretaker).status_code, 404)

        self.client.force_login(self.landlord)
        self.assertEqual(self.client.get(reverse('impersonate')).status_code, 403)
        self.assertEqual(self.client.post(reverse('impersonate_start', args=[self.landlord.pk]),
                                          {'reason': 'x'}).status_code, 403)

    def test_a_forged_session_does_nothing(self):
        self.client.force_login(self.landlord)
        session = self.client.session
        session[SESSION_KEY] = {'id': self.lease.tenant.owner_id, 'by': self.admin.pk, 'started': time.time()}
        session.save()
        self.assertNotContains(self.client.get(reverse('rentals:dashboard')), "you're viewing")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_private_log_is_superadmin_only_and_read_only(self):
        self.start()
        self.client.post(reverse('impersonate_stop'))
        self.assertEqual(self.client.get('/admin/accounts/supportsession/').status_code, 200)
        log = SupportSession.objects.get()
        self.assertEqual(self.client.post(f'/admin/accounts/supportsession/{log.pk}/delete/', {'post': 'yes'}).status_code,
                         403)
        self.assertTrue(SupportSession.objects.exists())
