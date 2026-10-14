"""Voiding, moving payments, the audit trail, and STK status checks."""

import datetime
import json
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEntry
from rentals.models import RentCharge
from rentals.tests import make_lease

from . import mpesa
from .models import C2BPayment, MpesaTransaction, Payment
from .tests import MPESA_SETTINGS, c2b_body, make_account, mock_daraja, success_callback

D = Decimal


class VoidAndMoveTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner)
        self.lease.ensure_charge_for(datetime.date(2026, 3, 1))  # 15,000 owed
        self.client.force_login(self.owner)

    def test_void_cash_payment(self):
        p = Payment.objects.create(lease=self.lease, amount=D('5000'), method='cash', reference='R1')
        url = reverse('payments:payment_void', args=[p.pk])
        self.assertEqual(self.client.post(url, {'reason': ''}).status_code, 200)  # reason required
        self.assertEqual(self.lease.balance, D('10000'))

        resp = self.client.post(url, {'reason': 'Entered twice'})
        self.assertRedirects(resp, reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.assertEqual(self.lease.balance, D('15000'))
        p = Payment.all_objects.get(pk=p.pk)
        self.assertEqual((p.voided_by, p.void_reason), (self.owner, 'Entered twice'))
        entry = AuditEntry.objects.get(action='payment.voided')
        self.assertEqual((entry.actor, entry.lease), (self.owner, self.lease))

        page = self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.assertContains(page, 'Entered twice')  # shown in the Voided section
        self.assertEqual(self.client.get(reverse('reports:payment_receipt', args=[p.pk])).status_code, 404)
        # The same reference can be entered again correctly.
        Payment.objects.create(lease=self.lease, amount=D('500'), method='cash', reference='R1')

    def test_mpesa_payments_cannot_be_voided(self):
        p = Payment.objects.create(lease=self.lease, amount=D('5000'), method='mpesa', reference='SJK1')
        self.client.post(reverse('payments:payment_void', args=[p.pk]), {'reason': 'x'})
        self.assertFalse(Payment.objects.get(pk=p.pk).is_voided)

    def test_move_mpesa_payment(self):
        other = make_lease(self.owner, label='A2')
        account = make_account(self.owner)
        p = Payment.objects.create(lease=self.lease, amount=D('5000'), method='mpesa', reference='SJK1')
        C2BPayment.objects.create(account=account, trans_id='SJK1', amount=5000, trans_time=timezone.now(),
                                  raw={}, lease=self.lease, payment=p)
        resp = self.client.post(reverse('payments:payment_move', args=[p.pk]),
                                {'lease': other.pk, 'reason': 'Paid for her sister'})
        self.assertRedirects(resp, reverse('rentals:lease_detail', args=[other.pk]))
        self.assertEqual(other.total_paid, D('5000'))
        self.assertEqual(self.lease.total_paid, 0)
        self.assertEqual(C2BPayment.objects.get().lease, other)
        self.assertEqual(AuditEntry.objects.filter(action='payment.moved').count(), 2)  # on both leases

    def test_cannot_move_to_other_landlord_or_touch_their_payments(self):
        bob_lease = make_lease(get_user_model().objects.create_user('bob'), label='B1')
        p = Payment.objects.create(lease=self.lease, amount=D('5000'), method='mpesa', reference='SJK1')
        self.client.post(reverse('payments:payment_move', args=[p.pk]), {'lease': bob_lease.pk, 'reason': 'x'})
        self.assertEqual(Payment.objects.get(pk=p.pk).lease, self.lease)
        bob_payment = Payment.objects.create(lease=bob_lease, amount=1, method='cash')
        self.assertEqual(self.client.get(reverse('payments:payment_void', args=[bob_payment.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('payments:payment_move', args=[bob_payment.pk])).status_code, 404)

    def test_void_charge(self):
        charge = self.lease.charges.get(kind='rent')
        self.client.post(reverse('rentals:charge_void', args=[charge.pk]), {'reason': 'Moved in late'})
        self.assertEqual(self.lease.balance, 0)
        # A voided monthly charge isn't billed again by the monthly run.
        _, created = self.lease.ensure_charge_for(datetime.date(2026, 3, 1))
        self.assertFalse(created)
        self.assertEqual(self.lease.balance, 0)

    def test_voided_late_fee_is_not_re_added(self):
        self.lease.late_fee = D('1000')
        self.lease.save()
        fee = self.lease.apply_late_fee(datetime.date(2026, 3, 20))
        fee.void(self.owner, 'Waived')
        self.assertIsNone(self.lease.apply_late_fee(datetime.date(2026, 3, 25)))

    def test_reports_exclude_voided(self):
        p = Payment.objects.create(lease=self.lease, amount=D('5000'), method='cash')
        p.void(self.owner, 'mistake')
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(RentCharge.objects.count(), 1)


class AuditTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner)
        self.client.force_login(self.owner)

    def test_lease_edit_records_before_and_after(self):
        url = reverse('rentals:lease_update', args=[self.lease.pk])
        self.client.post(url, {
            'unit': self.lease.unit.pk, 'tenant': self.lease.tenant.pk, 'start_date': '2026-01-15',
            'monthly_rent': '16000', 'deposit': '0', 'due_day': '5',
        })
        entry = AuditEntry.objects.get(action='lease.edited')
        self.assertEqual(entry.changes['Monthly rent (KES)'], ['15000.00', '16000'])
        self.assertEqual(entry.lease, self.lease)

    def test_activity_page_is_scoped(self):
        AuditEntry.objects.create(owner=self.owner, action='x', summary='Mine')
        AuditEntry.objects.create(owner=get_user_model().objects.create_user('bob'), action='x', summary='Bobs')
        resp = self.client.get(reverse('audit:activity'))
        self.assertContains(resp, 'Mine')
        self.assertNotContains(resp, 'Bobs')

    def test_mpesa_settings_change_masks_secrets(self):
        from accounts.verification import mark_verified

        mark_verified(self.owner)
        self.client.post(reverse('payments:mpesa_settings'), {
            'action': 'save', 'environment': 'sandbox', 'payment_type': 'paybill', 'shortcode': '174379',
            'consumer_key': 'ck-secret', 'consumer_secret': 'cs-secret', 'passkey': 'pk-secret',
        })
        entry = AuditEntry.objects.get(action='mpesa.settings_changed')
        logged = json.dumps(entry.changes)
        for value in ('ck-secret', 'cs-secret', 'pk-secret'):
            self.assertNotIn(value, logged)
        self.assertEqual(entry.changes['Passkey'][1], '••• (changed)')


@override_settings(MPESA=MPESA_SETTINGS)
class StkStatusCheckTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.account = make_account(self.owner)
        self.lease = make_lease(self.owner)
        self.txn = MpesaTransaction.objects.create(
            account=self.account, lease=self.lease, phone='254712345678', amount=7500, checkout_request_id='ws_CO_7',
        )
        MpesaTransaction.objects.filter(pk=self.txn.pk).update(created_at=timezone.now() - datetime.timedelta(minutes=5))
        self.txn.refresh_from_db()

    def query(self, req, response, status=200):
        mock_daraja(req, response, status=status)
        return mpesa.query_stk_status(self.txn)

    @mock.patch('payments.mpesa.requests')
    def test_paid_credits_tenant_without_receipt(self, req):
        txn = self.query(req, {'ResponseCode': '0', 'ResultCode': '0', 'ResultDesc': 'processed successfully'})
        self.assertEqual(txn.status, 'success')
        payment = txn.payment
        self.assertEqual((payment.amount, payment.reference, payment.lease), (D('7500'), '', self.lease))
        self.assertEqual(req.post.call_args.kwargs['json']['CheckoutRequestID'], 'ws_CO_7')

    @mock.patch('payments.mpesa.requests')
    def test_late_callback_fills_receipt_without_double_credit(self, req):
        self.query(req, {'ResponseCode': '0', 'ResultCode': '0'})
        self.client.post(reverse('payments:stk_callback', args=[self.account.callback_token]),
                         json.dumps(success_callback('ws_CO_7', amount=7500, receipt='SLATE1')),
                         content_type='application/json')
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Payment.objects.get().reference, 'SLATE1')
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.receipt_number, 'SLATE1')

    @mock.patch('payments.mpesa.requests')
    def test_late_c2b_confirmation_claims_payment(self, req):
        self.query(req, {'ResponseCode': '0', 'ResultCode': '0'})
        body = c2b_body(trans_id='SC2B1', amount='7500.00', bill_ref=self.lease.account_number)
        body['TransTime'] = timezone.localtime(self.txn.created_at).strftime('%Y%m%d%H%M%S')
        self.client.post(reverse('payments:c2b_confirmation', args=[self.account.callback_token]),
                         json.dumps(body), content_type='application/json')
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Payment.objects.get().reference, 'SC2B1')
        self.assertEqual(self.lease.total_paid, D('7500'))

    @mock.patch('payments.mpesa.requests')
    def test_cancelled(self, req):
        txn = self.query(req, {'ResponseCode': '0', 'ResultCode': '1032', 'ResultDesc': 'Request cancelled by user'})
        self.assertEqual((txn.status, txn.result_code), ('failed', 1032))
        self.assertFalse(Payment.objects.exists())

    @mock.patch('payments.mpesa.requests')
    def test_still_processing_then_expires(self, req):
        processing = {'errorCode': '500.001.1001', 'errorMessage': 'The transaction is being processed'}
        self.assertEqual(self.query(req, processing, status=500).status, 'pending')
        MpesaTransaction.objects.filter(pk=self.txn.pk).update(created_at=timezone.now() - datetime.timedelta(hours=25))
        self.txn.refresh_from_db()
        txn = self.query(req, processing, status=500)
        self.assertEqual(txn.status, 'failed')
        self.assertIn('24 hours', txn.result_desc)

    @mock.patch('payments.mpesa.requests')
    def test_callback_that_already_arrived_wins(self, req):
        self.client.post(reverse('payments:stk_callback', args=[self.account.callback_token]),
                         json.dumps(success_callback('ws_CO_7', amount=7500, receipt='SCB1')),
                         content_type='application/json')
        self.txn.refresh_from_db()
        self.query(req, {'ResponseCode': '0', 'ResultCode': '0'})
        self.assertEqual(Payment.objects.count(), 1)
        req.post.assert_not_called()  # nothing pending, nothing to ask

    @mock.patch('payments.mpesa.requests')
    def test_command_checks_only_old_pending(self, req):
        MpesaTransaction.objects.create(account=self.account, lease=self.lease, phone='254712345678',
                                        amount=1, checkout_request_id='ws_CO_new')  # just sent: skip
        mock_daraja(req, {'ResponseCode': '0', 'ResultCode': '0'})
        out = StringIO()
        call_command('check_pending_stk', stdout=out)
        self.assertIn('Checked 1: 1 paid', out.getvalue())

    @mock.patch('payments.mpesa.requests')
    def test_check_button_is_scoped(self, req):
        mock_daraja(req, {'ResponseCode': '0', 'ResultCode': '0'})
        intruder = get_user_model().objects.create_user('intruder')
        self.client.force_login(intruder)
        self.assertEqual(self.client.post(reverse('payments:stk_check', args=[self.txn.pk])).status_code, 404)
        self.client.force_login(self.owner)
        resp = self.client.post(reverse('payments:stk_check', args=[self.txn.pk]), follow=True)
        self.assertContains(resp, 'has been credited')
