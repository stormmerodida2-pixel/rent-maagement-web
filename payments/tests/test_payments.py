import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.utils import mark_verified
from rentals.management.commands.create_landlord import landlord_group
from rentals.tests import make_lease

from .. import mpesa
from ..models import C2BPayment, MpesaAccount, MpesaTransaction, Payment
from . import MPESA_SETTINGS, c2b_body, make_account, mock_daraja, success_callback

class NormalizePhoneTests(TestCase):
    def test_formats(self):
        for raw in ['0712345678', '+254712345678', '254712345678', '712345678', '0712 345 678']:
            self.assertEqual(mpesa.normalize_phone(raw), '254712345678')
        self.assertEqual(mpesa.normalize_phone('0110123456'), '254110123456')

    def test_invalid(self):
        for raw in ['', '12345', '0812345678', '07123456789']:
            with self.assertRaises(mpesa.MpesaError):
                mpesa.normalize_phone(raw)


class MpesaAccountTests(TestCase):
    def test_secrets_are_encrypted_at_rest(self):
        account = make_account(get_user_model().objects.create_user('landlord'))
        with connection.cursor() as cursor:
            cursor.execute('SELECT consumer_secret, passkey FROM payments_mpesaaccount WHERE id = %s', [account.pk])
            raw_secret, raw_passkey = cursor.fetchone()
        self.assertNotIn('secret-landlord', raw_secret)
        self.assertNotEqual(raw_passkey, 'passkey')
        account = MpesaAccount.objects.get(pk=account.pk)
        self.assertEqual((account.consumer_secret, account.passkey), ('secret-landlord', 'passkey'))

    def test_wrong_key_reads_as_blank_instead_of_crashing(self):
        account = make_account(get_user_model().objects.create_user('landlord'))
        with self.settings(FIELD_ENCRYPTION_KEY='x' * 43 + '='):
            account = MpesaAccount.objects.get(pk=account.pk)
        self.assertEqual(account.passkey, '')
        self.assertFalse(account.is_complete())


@override_settings(MPESA=MPESA_SETTINGS)
class StkPushTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner)

    @mock.patch('payments.mpesa.requests')
    def test_requires_connected_account(self, req):
        with self.assertRaisesMessage(mpesa.MpesaError, "isn't connected"):
            mpesa.stk_push(self.lease, 100)
        req.post.assert_not_called()

    @mock.patch('payments.mpesa.requests')
    def test_uses_landlords_own_credentials(self, req):
        account = make_account(self.owner, shortcode='174379')
        mock_daraja(req, {'MerchantRequestID': 'm-1', 'CheckoutRequestID': 'ws_CO_1', 'ResponseCode': '0'})

        txn = mpesa.stk_push(self.lease, Decimal('1500.40'))

        self.assertEqual((txn.status, txn.account), (MpesaTransaction.Status.PENDING, account))
        self.assertEqual(txn.amount, Decimal('1501'))  # rounded up to whole shillings
        self.assertEqual(req.get.call_args.kwargs['auth'], ('key-landlord', 'secret-landlord'))
        payload = req.post.call_args.kwargs['json']
        self.assertEqual((payload['BusinessShortCode'], payload['PartyB']), ('174379', '174379'))
        self.assertEqual(payload['TransactionType'], 'CustomerPayBillOnline')
        self.assertEqual(payload['Amount'], 1501)
        self.assertEqual(
            payload['CallBackURL'], f'https://example.ngrok.app/payments/hooks/{account.callback_token}/stk/'
        )

    @mock.patch('payments.mpesa.requests')
    def test_till_uses_buy_goods(self, req):
        make_account(self.owner, payment_type='till', shortcode='7654321', till_number='123456')
        mock_daraja(req, {'CheckoutRequestID': 'ws_CO_1', 'ResponseCode': '0'})
        mpesa.stk_push(self.lease, 100)
        payload = req.post.call_args.kwargs['json']
        self.assertEqual(payload['TransactionType'], 'CustomerBuyGoodsOnline')
        self.assertEqual((payload['BusinessShortCode'], payload['PartyB']), ('7654321', '123456'))

    @mock.patch('payments.mpesa.requests')
    def test_rejected_push_raises(self, req):
        make_account(self.owner)
        mock_daraja(req, {'errorMessage': 'Invalid PhoneNumber'}, status=400)
        with self.assertRaisesMessage(mpesa.MpesaError, 'Invalid PhoneNumber'):
            mpesa.stk_push(self.lease, 100)
        self.assertFalse(MpesaTransaction.objects.exists())

    @mock.patch('payments.mpesa.requests')
    def test_access_tokens_are_cached_per_account(self, req):
        other = get_user_model().objects.create_user('other')
        a, b = make_account(self.owner), make_account(other)
        mock_daraja(req, {})
        mpesa.get_access_token(a)
        mpesa.get_access_token(a)
        mpesa.get_access_token(b)
        self.assertEqual(req.get.call_count, 2)


@override_settings(MPESA=MPESA_SETTINGS)
class CallbackTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.account = make_account(self.owner)
        self.lease = make_lease(self.owner)
        self.txn = MpesaTransaction.objects.create(
            account=self.account, lease=self.lease, phone='254712345678', amount=15000,
            checkout_request_id='ws_CO_1',
        )
        self.url = reverse('payments:stk_callback', args=[self.account.callback_token])

    def post(self, body, url=None):
        return self.client.post(url or self.url, json.dumps(body), content_type='application/json')

    def test_success_records_payment_once(self):
        self.assertEqual(self.post(success_callback('ws_CO_1')).json()['ResultCode'], 0)
        self.post(success_callback('ws_CO_1'))  # Safaricom retry

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, MpesaTransaction.Status.SUCCESS)
        self.assertEqual(Payment.objects.count(), 1)
        payment = self.txn.payment
        self.assertEqual((payment.amount, payment.reference, payment.lease), (Decimal('15000'), 'SJK1ABC2DE', self.lease))
        self.assertEqual(payment.paid_at.strftime('%Y%m%d%H%M%S'), '20261013113015')  # 14:30 EAT in UTC

    def test_cancelled_by_user(self):
        self.post({'Body': {'stkCallback': {
            'MerchantRequestID': 'm-1', 'CheckoutRequestID': 'ws_CO_1',
            'ResultCode': 1032, 'ResultDesc': 'Request cancelled by user',
        }}})
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, MpesaTransaction.Status.FAILED)
        self.assertEqual(self.txn.result_code, 1032)
        self.assertFalse(Payment.objects.exists())

    def test_unknown_token_is_404(self):
        resp = self.post(success_callback('ws_CO_1'), url=reverse('payments:stk_callback', args=['nope']))
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(Payment.objects.exists())

    def test_another_landlords_token_cannot_complete_this_transaction(self):
        other = make_account(get_user_model().objects.create_user('other'))
        self.post(success_callback('ws_CO_1'), url=reverse('payments:stk_callback', args=[other.callback_token]))
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, MpesaTransaction.Status.PENDING)
        self.assertFalse(Payment.objects.exists())

    def test_unknown_checkout_and_malformed_bodies_are_acknowledged(self):
        with self.assertLogs('payments', level='WARNING') as logs:
            self.assertEqual(self.post(success_callback('ws_CO_unknown')).status_code, 200)
            self.assertEqual(self.post({'junk': True}).status_code, 200)
        self.assertFalse(Payment.objects.exists())
        self.assertTrue(any('unknown CheckoutRequestID' in line for line in logs.output))
        self.assertTrue(any('Malformed STK callback' in line for line in logs.output))


@override_settings(MPESA=MPESA_SETTINGS)
class RequestPaymentViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user('alice', password='pw')
        self.bob = User.objects.create_user('bob', password='pw')
        self.bob_lease = make_lease(self.bob)

    @mock.patch('payments.mpesa.stk_push')
    def test_cannot_charge_another_landlords_tenant(self, stk_push):
        self.client.login(username='alice', password='pw')
        resp = self.client.post(reverse('payments:request_payment', args=[self.bob_lease.pk]), {'amount': '100'})
        self.assertEqual(resp.status_code, 404)
        stk_push.assert_not_called()


@override_settings(MPESA=MPESA_SETTINGS)
class C2BTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.account = make_account(self.owner)
        self.lease = make_lease(self.owner)
        self.url = reverse('payments:c2b_confirmation', args=[self.account.callback_token])

    def confirm(self, body, url=None):
        return self.client.post(url or self.url, json.dumps(body), content_type='application/json')

    def test_matches_by_account_number_variants(self):
        for i, ref in enumerate([f'LEASE{self.lease.pk}', f'lease-{self.lease.pk}', f' Lease {self.lease.pk} ']):
            self.confirm(c2b_body(trans_id=f'T{i}', bill_ref=ref, msisdn='hashed'))
        self.assertEqual(Payment.objects.filter(lease=self.lease).count(), 3)
        self.assertEqual(self.lease.total_paid, Decimal('22500'))
        self.assertEqual(set(C2BPayment.objects.values_list('account', flat=True)), {self.account.pk})

    def test_falls_back_to_phone_and_is_idempotent(self):
        self.confirm(c2b_body(bill_ref='house A1'))
        self.confirm(c2b_body(bill_ref='house A1'))  # retry
        c2b = C2BPayment.objects.get()
        self.assertEqual(c2b.lease, self.lease)
        self.assertEqual(c2b.payer_name, 'Jane Doe')
        self.assertEqual(Payment.objects.get().reference, 'SJK9XYZ123')

    def test_never_credits_another_landlords_lease(self):
        other = get_user_model().objects.create_user('other')
        other_lease = make_lease(other, label='Z9')
        # Money paid into *this* landlord's Paybill quoting the other landlord's account number.
        self.confirm(c2b_body(bill_ref=other_lease.account_number, msisdn='hashed'))
        c2b = C2BPayment.objects.get()
        self.assertIsNone(c2b.lease)
        self.assertFalse(Payment.objects.exists())

    def test_landlord_assigns_own_unmatched_payment_in_admin(self):
        self.confirm(c2b_body(bill_ref='??', msisdn='hashed-value'))
        c2b = C2BPayment.objects.get()
        self.assertIsNone(c2b.lease)

        self.owner.is_staff = True
        self.owner.save()
        self.owner.groups.add(landlord_group())
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('admin:payments_c2bpayment_changelist')), c2b.trans_id)
        resp = self.client.post(reverse('admin:payments_c2bpayment_change', args=[c2b.pk]), {'lease': self.lease.pk})
        self.assertEqual(resp.status_code, 302)
        c2b.refresh_from_db()
        self.assertEqual((c2b.payment.lease, c2b.payment.amount), (self.lease, Decimal('7500')))

    def test_cannot_assign_to_another_landlords_lease(self):
        self.confirm(c2b_body(bill_ref='??', msisdn='hashed-value'))
        c2b = C2BPayment.objects.get()
        other_lease = make_lease(get_user_model().objects.create_user('other'), label='Z9')
        self.client.force_login(get_user_model().objects.create_superuser('root', password='pw'))
        resp = self.client.post(reverse('admin:payments_c2bpayment_change', args=[c2b.pk]), {'lease': other_lease.pk})
        self.assertContains(resp, "another landlord")
        self.assertFalse(Payment.objects.exists())

    def test_other_landlords_dont_see_my_payments(self):
        self.confirm(c2b_body(bill_ref='??', msisdn='hashed-value'))
        other = get_user_model().objects.create_user('other', is_staff=True)
        other.groups.add(landlord_group())
        self.client.force_login(other)
        self.assertNotContains(self.client.get(reverse('admin:payments_c2bpayment_changelist')), 'SJK9XYZ123')

    def test_stk_and_c2b_for_same_receipt_create_one_payment(self):
        MpesaTransaction.objects.create(
            account=self.account, lease=self.lease, phone='254712345678', amount=7500, checkout_request_id='ws_CO_9'
        )
        self.confirm(c2b_body(trans_id='SAME123', bill_ref=self.lease.account_number))
        self.client.post(
            reverse('payments:stk_callback', args=[self.account.callback_token]),
            json.dumps(success_callback('ws_CO_9', amount=7500, receipt='SAME123')),
            content_type='application/json',
        )
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(MpesaTransaction.objects.get().payment, Payment.objects.get())

    def test_validation_rejects_unknown_account(self):
        url = reverse('payments:c2b_validation', args=[self.account.callback_token])
        bad = self.client.post(url, json.dumps(c2b_body(bill_ref='nope', msisdn='hashed')), content_type='application/json')
        good = self.client.post(url, json.dumps(c2b_body(bill_ref=self.lease.account_number)), content_type='application/json')
        self.assertEqual(bad.json()['ResultCode'], 'C2B00012')
        self.assertEqual(good.json()['ResultCode'], 0)

    def test_payment_sends_receipt_sms_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.confirm(c2b_body(bill_ref=self.lease.account_number))
        msg = self.lease.sms_messages.get()
        self.assertEqual(msg.kind, 'receipt')
        self.assertIn('KES 7,500', msg.body)
        self.assertIn('in credit', msg.body)

    @mock.patch('payments.mpesa.requests')
    def test_register_c2b_urls(self, req):
        cache.clear()
        mock_daraja(req, {'ResponseCode': '0', 'ResponseDescription': 'success'})
        mpesa.register_c2b_urls(self.account)
        payload = req.post.call_args.kwargs['json']
        self.assertEqual(payload['ShortCode'], '600986')
        self.assertEqual(
            payload['ConfirmationURL'],
            f'https://example.ngrok.app/payments/hooks/{self.account.callback_token}/c2b/confirmation/',
        )
        self.assertNotIn('mpesa', payload['ConfirmationURL'].lower())
        self.account.refresh_from_db()
        self.assertIsNotNone(self.account.c2b_registered_at)


@override_settings(MPESA=MPESA_SETTINGS)
class MpesaSettingsViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user('landlord', password='pw')
        mark_verified(self.user)
        self.client.force_login(self.user)
        self.url = reverse('payments:mpesa_settings')

    def save(self, **overrides):
        data = {
            'action': 'save', 'environment': 'sandbox', 'payment_type': 'paybill', 'shortcode': '174379',
            'till_number': '', 'consumer_key': 'ck', 'consumer_secret': 'cs', 'passkey': 'pk', **overrides,
        }
        return self.client.post(self.url, data)

    def test_save_creates_account_and_never_echoes_secrets(self):
        self.assertEqual(self.save().status_code, 302)
        account = self.user.mpesa_account
        self.assertEqual((account.consumer_key, account.consumer_secret, account.passkey), ('ck', 'cs', 'pk'))
        page = self.client.get(self.url)
        self.assertNotContains(page, 'value="cs"')
        self.assertNotContains(page, 'value="pk"')
        self.assertContains(page, account.callback_token)

    def test_blank_secret_keeps_saved_value_and_verified_status(self):
        self.save()
        account = self.user.mpesa_account
        account.verified_at = account.created_at
        account.save()
        self.save(consumer_key='', consumer_secret='', passkey='')
        account.refresh_from_db()
        self.assertEqual(account.consumer_secret, 'cs')
        self.assertIsNotNone(account.verified_at)
        self.save(consumer_secret='new')
        account.refresh_from_db()
        self.assertEqual(account.consumer_secret, 'new')
        self.assertIsNone(account.verified_at)

    def test_unverified_email_cannot_connect_mpesa(self):
        self.user.profile.email_verified_at = None
        self.user.profile.save()
        resp = self.save()
        self.assertRedirects(resp, self.url)
        self.assertFalse(MpesaAccount.objects.exists())

    def test_till_requires_till_number(self):
        resp = self.save(payment_type='till')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(MpesaAccount.objects.exists())

    @mock.patch('payments.mpesa.requests')
    def test_verify_records_success_and_failure(self, req):
        self.save()
        mock_daraja(req, {})
        self.client.post(self.url, {'action': 'verify'})
        self.assertIsNotNone(MpesaAccount.objects.get().verified_at)

        req.get.return_value.status_code = 401
        self.client.post(self.url, {'action': 'verify'})
        account = MpesaAccount.objects.get()
        self.assertIsNone(account.verified_at)
        self.assertIn('rejected', account.last_error)


class PaymentPagesTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user('alice')
        self.lease = make_lease(self.alice)
        self.client.force_login(self.alice)

    def test_record_cash_payment(self):
        resp = self.client.post(reverse('payments:payment_create'), {
            'lease': self.lease.pk, 'amount': '5000', 'method': 'cash', 'reference': '',
            'paid_at': '2026-10-10T09:30', 'notes': '',
        })
        self.assertRedirects(resp, reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.assertEqual(self.lease.total_paid, Decimal('5000'))

    def test_cannot_record_mpesa_by_hand_or_for_other_landlord(self):
        bob_lease = make_lease(get_user_model().objects.create_user('bob'), label='B1')
        url = reverse('payments:payment_create')
        base = {'amount': '5000', 'reference': 'X', 'paid_at': '2026-10-10T09:30'}
        self.client.post(url, {**base, 'lease': self.lease.pk, 'method': 'mpesa'})
        self.client.post(url, {**base, 'lease': bob_lease.pk, 'method': 'cash'})
        self.assertFalse(Payment.objects.exists())

    def test_list_filters_and_scoping(self):
        Payment.objects.create(lease=self.lease, amount=100, method='cash')
        Payment.objects.create(lease=self.lease, amount=200, method='bank')
        Payment.objects.create(lease=make_lease(get_user_model().objects.create_user('bob'), label='B1'),
                               amount=999, method='cash')
        resp = self.client.get(reverse('payments:payment_list'))
        self.assertEqual(resp.context['total'], Decimal('300'))
        self.assertEqual(self.client.get(reverse('payments:payment_list') + '?method=bank').context['total'], Decimal('200'))

    def test_assign_unmatched_from_app(self):
        account = make_account(self.alice)
        c2b = C2BPayment.objects.create(account=account, trans_id='UNM1', amount=4000,
                                        trans_time=self.lease.start_date.isoformat() + 'T10:00Z', raw={})
        self.assertContains(self.client.get(reverse('payments:unmatched_list')), 'UNM1')
        resp = self.client.post(reverse('payments:assign_unmatched', args=[c2b.pk]), {f'c{c2b.pk}-lease': self.lease.pk})
        self.assertRedirects(resp, reverse('payments:unmatched_list'))
        c2b.refresh_from_db()
        self.assertEqual((c2b.lease, c2b.payment.amount), (self.lease, Decimal('4000')))

    def test_cannot_assign_other_landlords_unmatched(self):
        bob = get_user_model().objects.create_user('bob')
        c2b = C2BPayment.objects.create(account=make_account(bob), trans_id='UNM2', amount=1,
                                        trans_time='2026-10-10T10:00Z', raw={})
        resp = self.client.post(reverse('payments:assign_unmatched', args=[c2b.pk]), {f'c{c2b.pk}-lease': self.lease.pk})
        self.assertEqual(resp.status_code, 404)
