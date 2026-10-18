"""Administrators see every landlord and impersonate one with a click; the landlord isn't told, but a private log
keeps every session."""

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


class LandlordsPageTests(TestCase):
    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.admin = User.objects.create_superuser('boss', password='pw', first_name='Stormer')
        self.active = User.objects.create_user('grace', first_name='Grace', last_name='Wanjiru', email='g@example.com')
        make_lease(self.active, label='A1')
        self.new = User.objects.create_user('otieno', first_name='Otieno', email='o@example.com')
        caretaker = User.objects.create_user('peter', first_name='Peter')
        StaffMember.objects.create(owner=self.active, user=caretaker)
        self.client.force_login(self.admin)

    def test_lists_landlords_only_with_an_impersonate_button_each(self):
        page = self.client.get(reverse('admin_landlords'))
        for text in ('Grace Wanjiru', 'Otieno', '1 of 1', 'None yet',
                     reverse('impersonate_start', args=[self.active.pk]),
                     reverse('impersonate_start', args=[self.new.pk])):
            self.assertContains(page, text)
        self.assertNotContains(page, 'Peter')   # caretakers aren't landlords
        self.assertNotContains(page, reverse('impersonate_start', args=[self.admin.pk]))  # nor are administrators
        self.assertEqual(page.context['counts'], {'all': 2, 'using': 1, 'new': 1})

    def test_filters_and_search(self):
        self.assertNotContains(self.client.get(reverse('admin_landlords') + '?show=new'), 'Grace')
        self.assertNotContains(self.client.get(reverse('admin_landlords') + '?show=using'), 'Otieno')
        self.assertNotContains(self.client.get(reverse('admin_landlords') + '?q=wanjiru'), 'Otieno')

    def test_administrators_only(self):
        self.client.force_login(self.active)
        self.assertEqual(self.client.get(reverse('admin_landlords')).status_code, 403)
        self.assertEqual(self.client.post(reverse('impersonate_start', args=[self.new.pk])).status_code, 403)
        self.assertNotContains(self.client.get(reverse('rentals:dashboard')), reverse('admin_landlords'))


class ImpersonationTests(TestCase):
    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.admin = User.objects.create_superuser('boss', password='pw', first_name='Stormer')
        self.landlord = User.objects.create_user('landlord', first_name='Grace', last_name='Wanjiru')
        self.lease = make_lease(self.landlord)
        self.client.force_login(self.admin)

    def start(self, who=None):
        return self.client.post(reverse('impersonate_start', args=[(who or self.landlord).pk]))

    def test_one_click_and_you_are_them(self):
        self.assertRedirects(self.start(), reverse('rentals:dashboard'), fetch_redirect_response=False)
        dash = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(dash, 'Impersonating Grace Wanjiru')
        self.assertContains(self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk])), str(self.lease.tenant))
        # Not told: nothing in their Activity. The private log has it.
        self.assertFalse(AuditEntry.objects.filter(owner=self.landlord, action__startswith='support').exists())
        log = SupportSession.objects.get()
        self.assertEqual((log.superadmin, log.landlord, log.ended_at), (self.admin, self.landlord, None))

    def test_can_do_what_they_can_logged_as_support(self):
        self.start()
        self.client.post(reverse('rentals:property_create'), {'name': 'Hillview'})
        self.assertEqual(Property.objects.get(name='Hillview').owner, self.landlord)
        payment = Payment.objects.create(lease=self.lease, amount=Decimal('100'), method='cash')
        self.client.post(reverse('payments:payment_void', args=[payment.pk]), {'reason': 'Duplicate'})
        entry = AuditEntry.objects.filter(action__contains='void').latest('created_at')
        self.assertEqual((entry.owner, entry.actor, entry.actor_label), (self.landlord, self.admin, 'Rentipay support'))

    def test_their_password_and_two_step_stay_off_limits(self):
        self.start()
        for url in (reverse('accounts:password_change'), reverse('accounts:two_factor')):
            self.assertRedirects(self.client.get(url), reverse('rentals:dashboard'), fetch_redirect_response=False)

    def test_stop_returns_to_the_list(self):
        self.start()
        resp = self.client.post(reverse('impersonate_stop'))
        self.assertRedirects(resp, reverse('admin_landlords'))
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertIsNotNone(SupportSession.objects.get().ended_at)
        self.assertNotContains(self.client.get(reverse('rentals:dashboard')), 'Impersonating')

    def test_ends_after_an_hour(self):
        self.start()
        session = self.client.session
        session[SESSION_KEY]['started'] = time.time() - LIMIT - 1
        session.save()
        self.assertNotContains(self.client.get(reverse('rentals:dashboard'), follow=True), 'Impersonating')
        self.assertIsNotNone(SupportSession.objects.get().ended_at)

    def test_never_another_admin_or_a_caretaker(self):
        other_admin = get_user_model().objects.create_superuser('boss2', password='pw')
        self.assertEqual(self.start(who=other_admin).status_code, 404)
        caretaker = get_user_model().objects.create_user('peter')
        StaffMember.objects.create(owner=self.landlord, user=caretaker)
        self.assertEqual(self.start(who=caretaker).status_code, 404)

    def test_a_forged_session_does_nothing(self):
        self.client.force_login(self.landlord)
        session = self.client.session
        session[SESSION_KEY] = {'id': self.landlord.pk, 'by': self.admin.pk, 'started': time.time()}
        session.save()
        self.assertNotContains(self.client.get(reverse('rentals:dashboard')), 'Impersonating')
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_private_log_is_read_only(self):
        self.start()
        self.client.post(reverse('impersonate_stop'))
        self.assertEqual(self.client.get('/admin/accounts/supportsession/').status_code, 200)
        log = SupportSession.objects.get()
        self.assertEqual(self.client.post(f'/admin/accounts/supportsession/{log.pk}/delete/', {'post': 'yes'}).status_code,
                         403)


class SwitchingTests(TestCase):
    """Mid-impersonation, the Landlords page and its buttons still work as the superadmin."""

    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.admin = User.objects.create_superuser('boss', password='pw')
        self.grace = User.objects.create_user('grace', first_name='Grace')
        self.otieno = User.objects.create_user('otieno', first_name='Otieno')
        make_lease(self.grace)
        make_lease(self.otieno, label='B1')
        self.client.force_login(self.admin)

    def test_list_and_switch_while_impersonating(self):
        self.client.post(reverse('impersonate_start', args=[self.grace.pk]))
        page = self.client.get(reverse('admin_landlords'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "You're impersonating <strong>Grace</strong>")
        resp = self.client.post(reverse('impersonate_start', args=[self.otieno.pk]))
        self.assertRedirects(resp, reverse('rentals:dashboard'), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), 'Impersonating Otieno')
        first, second = SupportSession.objects.order_by('started_at')
        self.assertIsNotNone(first.ended_at)  # Grace's session was closed by the switch
        self.assertIsNone(second.ended_at)
