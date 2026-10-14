"""Staff (caretaker) accounts: what they can do, and everything they can't see."""

import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEntry
from maintenance.models import MaintenanceRequest
from notifications.models import SmsMessage
from payments.models import MpesaAccount, MpesaTransaction, Payment
from rentals.tests import make_lease

from .models import StaffMember

D = Decimal


class StaffTests(TestCase):
    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.owner = User.objects.create_user('landlord', password='pw')
        today = datetime.date.today()
        self.owes = make_lease(self.owner, label='A1', start=today.replace(day=1))
        self.owes.ensure_charge_for(today)
        self.paid = make_lease(self.owner, label='A2', start=today.replace(day=1))
        self.paid.ensure_charge_for(today)
        Payment.objects.create(lease=self.paid, amount=D('15000'), method='cash')
        self.stranger = make_lease(User.objects.create_user('other'), label='Z9')
        self.stranger.ensure_charge_for(today)
        MpesaAccount.objects.create(owner=self.owner, shortcode='174379', consumer_key='k', consumer_secret='s',
                                    passkey='p')

        self.client.force_login(self.owner)
        self.client.post(reverse('staff:add'), {'name': 'Peter Kamau', 'username': 'peter', 'phone': '0711000111',
                                                'password': 'Caretaker-2026'})
        self.member = StaffMember.objects.get()
        self.client.logout()
        self.client.login(username='peter', password='Caretaker-2026')

    # --- Setting staff up ---------------------------------------------------------------

    def test_landlord_adds_staff(self):
        self.assertEqual(self.member.owner, self.owner)
        self.assertEqual(self.member.name, 'Peter Kamau')
        self.assertEqual(self.member.phone, '254711000111')
        self.assertTrue(AuditEntry.objects.filter(action='staff.added', owner=self.owner).exists())
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('staff:manage')), 'Peter Kamau')
        self.assertContains(self.client.get(reverse('accounts:settings')), '1 staff member')

    def test_deactivated_staff_cannot_log_in(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('staff:edit', args=[self.member.pk]),
                         {'name': 'Peter Kamau', 'username': 'peter', 'phone': '', 'password': ''})  # active unticked
        self.client.logout()
        self.assertFalse(self.client.login(username='peter', password='Caretaker-2026'))

    def test_login_lands_in_the_staff_area(self):
        self.client.logout()
        resp = self.client.post(reverse('login'), {'username': 'peter', 'password': 'Caretaker-2026'}, follow=True)
        self.assertEqual(resp.redirect_chain[-1][0], reverse('staff:home'))

    # --- What staff can't see -----------------------------------------------------------

    def test_landlord_pages_are_off_limits(self):
        for url in [reverse('rentals:dashboard'), reverse('reports:overview'), reverse('payments:payment_list'),
                    reverse('reports:payments_csv'), reverse('accounts:settings'), reverse('staff:manage'),
                    reverse('rentals:lease_detail', args=[self.owes.pk]), reverse('rentals:live'),
                    reverse('payments:mpesa_settings'), reverse('audit:activity'), '/admin/']:
            resp = self.client.get(url)
            self.assertRedirects(resp, reverse('staff:home'), msg_prefix=url, fetch_redirect_response=False)

    def test_list_shows_who_owes_without_totals(self):
        page = self.client.get(reverse('staff:home'))
        self.assertContains(page, 'Tenant A1')
        self.assertContains(page, 'KES 15,000')  # what this tenant owes, to prompt the right amount
        self.assertNotContains(page, 'Tenant A2')  # paid up
        self.assertNotContains(page, 'Tenant Z9')  # another landlord's
        self.assertNotContains(page, 'Total')
        self.assertContains(self.client.get(reverse('staff:home'), {'show': 'all'}), 'Tenant A2')

    def test_limited_to_chosen_properties(self):
        other_property = make_lease(self.owner, label='B1')  # creates the same "landlord Apartments" property
        other_property.unit.property = type(other_property.unit.property).objects.create(owner=self.owner, name='Annex')
        other_property.unit.save()
        self.member.properties.set([self.owes.unit.property])
        page = self.client.get(reverse('staff:home'), {'show': 'all'})
        self.assertContains(page, 'Tenant A1')
        self.assertNotContains(page, 'Tenant B1')
        self.assertEqual(self.client.get(reverse('staff:record_cash', args=[other_property.pk])).status_code, 404)

    # --- M-Pesa prompts -----------------------------------------------------------------

    def fake_push(self, lease, amount, phone_number=None):
        return MpesaTransaction.objects.create(lease=lease, phone=lease.tenant.phone, amount=amount,
                                               checkout_request_id=f'ws_{lease.pk}_{amount}')

    def test_prompt_and_watch_for_the_payment(self):
        with mock.patch('staff.views.mpesa.stk_push', side_effect=self.fake_push) as push:
            resp = self.client.post(reverse('staff:send_prompt', args=[self.owes.pk]), {'amount': '15000'})
        push.assert_called_once()
        self.assertEqual(push.call_args.args[0], self.owes)  # the tenant's own number, never one staff typed
        txn = MpesaTransaction.objects.get()
        self.assertRedirects(resp, reverse('staff:prompt', args=[txn.pk]))
        url = reverse('staff:prompt', args=[txn.pk])
        self.assertEqual(self.client.get(url, {'format': 'json'}).json()['status'], 'waiting')
        txn.status, txn.receipt_number = MpesaTransaction.Status.SUCCESS, 'SJK123'
        txn.save()
        state = self.client.get(url, {'format': 'json'}).json()
        self.assertEqual(state['status'], 'paid')
        self.assertIn('SJK123', state['detail'])
        self.assertTrue(AuditEntry.objects.filter(action='staff.prompt_sent').exists())

    def test_cannot_prompt_someone_elses_tenant(self):
        with mock.patch('staff.views.mpesa.stk_push', side_effect=self.fake_push) as push:
            resp = self.client.post(reverse('staff:send_prompt', args=[self.stranger.pk]), {'amount': '100'})
        self.assertEqual(resp.status_code, 404)
        push.assert_not_called()

    # --- Cash ----------------------------------------------------------------------------

    def test_record_cash_sends_the_tenant_a_receipt(self):
        resp = self.client.post(reverse('staff:record_cash', args=[self.owes.pk]), {
            'amount': '5000', 'paid_at': timezone.localtime().strftime('%Y-%m-%dT%H:%M'), 'reference': '0042'})
        self.assertRedirects(resp, reverse('staff:home'))
        payment = Payment.objects.get(lease=self.owes)
        self.assertEqual((payment.method, payment.amount), ('cash', D('5000')))
        self.assertIn('Peter Kamau', payment.notes)
        sms = SmsMessage.objects.get(kind='receipt')
        self.assertIn('Received KES 5,000 cash for A1, recorded by Peter Kamau', sms.body)
        entry = AuditEntry.objects.get(action='payment.recorded')
        self.assertEqual((entry.owner, entry.actor_label), (self.owner, 'Peter Kamau (staff)'))

    def test_cash_dates_must_be_recent(self):
        for when in (timezone.localtime() + datetime.timedelta(days=1), timezone.localtime() - datetime.timedelta(days=30)):
            resp = self.client.post(reverse('staff:record_cash', args=[self.owes.pk]),
                                    {'amount': '5000', 'paid_at': when.strftime('%Y-%m-%dT%H:%M')})
            self.assertEqual(resp.status_code, 200)
        self.assertFalse(Payment.objects.filter(lease=self.owes).exists())

    # --- Repairs -------------------------------------------------------------------------

    def test_log_and_update_a_repair(self):
        self.client.post(reverse('staff:repair_create'), {'unit': self.owes.unit.pk, 'title': 'Leaking tap',
                                                          'priority': 'medium'})
        repair = MaintenanceRequest.objects.get()
        self.assertContains(self.client.get(reverse('staff:repairs')), 'Leaking tap')
        self.client.post(reverse('staff:repair_update', args=[repair.pk]), {
            'unit': self.owes.unit.pk, 'title': 'Leaking tap', 'priority': 'medium', 'status': 'resolved',
            'assigned_to': 'Fundi Joe', 'cost': '800'})
        repair.refresh_from_db()
        self.assertEqual((repair.status, repair.cost), ('resolved', D('800')))
        self.assertEqual(AuditEntry.objects.filter(action__startswith='repair.').count(), 2)

    def test_cannot_log_repairs_for_other_landlords(self):
        resp = self.client.post(reverse('staff:repair_create'), {'unit': self.stranger.unit.pk, 'title': 'x',
                                                                 'priority': 'low'})
        self.assertEqual(resp.status_code, 200)  # form error: not one of their units
        self.assertFalse(MaintenanceRequest.objects.exists())

    def test_staff_can_change_their_password(self):
        resp = self.client.post(reverse('staff:password'), {'old_password': 'Caretaker-2026',
                                                             'new_password1': 'New-Secret-2026',
                                                             'new_password2': 'New-Secret-2026'})
        self.assertRedirects(resp, reverse('staff:home'))

    def test_landlords_are_kept_out_of_the_staff_area(self):
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get(reverse('staff:home')), reverse('rentals:dashboard'),
                             fetch_redirect_response=False)
