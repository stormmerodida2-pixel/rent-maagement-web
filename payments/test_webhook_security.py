"""Webhooks are only trusted from Safaricom's addresses (production accounts)."""

import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from audit.models import AuditEntry
from rentals.tests import make_lease

from .models import C2BPayment, MpesaTransaction, Payment
from .tests import MPESA_SETTINGS, c2b_body, make_account, success_callback

SAFARICOM = '196.201.214.200'
ATTACKER = '6.6.6.6'


@override_settings(MPESA={**MPESA_SETTINGS, 'CALLBACK_IPS': [SAFARICOM, '196.201.212.0/24']})
class WebhookSourceTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.account = make_account(self.owner, environment='production')
        self.lease = make_lease(self.owner)
        self.c2b_url = reverse('payments:c2b_confirmation', args=[self.account.callback_token])

    def confirm(self, ip, **body):
        payload = c2b_body(bill_ref=self.lease.account_number, **body)
        return self.client.post(self.c2b_url, json.dumps(payload), content_type='application/json', REMOTE_ADDR=ip)

    def test_paybill_from_safaricom_is_credited(self):
        self.confirm(SAFARICOM)
        c2b = C2BPayment.objects.get()
        self.assertFalse(c2b.held_for_review)
        self.assertEqual((c2b.source_ip, self.lease.total_paid), (SAFARICOM, Decimal('7500')))

    def test_safaricom_range_is_accepted(self):
        self.confirm('196.201.212.138')
        self.assertEqual(self.lease.total_paid, Decimal('7500'))

    def test_fake_paybill_is_held_not_credited(self):
        self.assertEqual(self.confirm(ATTACKER).status_code, 200)  # nothing revealed to the sender
        c2b = C2BPayment.objects.get()
        self.assertTrue(c2b.held_for_review)
        self.assertIsNone(c2b.lease)
        self.assertFalse(Payment.objects.exists())
        self.assertTrue(AuditEntry.objects.filter(action='payment.held', owner=self.owner).exists())

        self.client.force_login(self.owner)
        page = self.client.get(reverse('payments:unmatched_list'))
        self.assertContains(page, 'Unverified, check statement')
        self.assertContains(page, "didn't come from Safaricom")

    def test_genuine_confirmation_releases_an_earlier_fake_copy(self):
        self.confirm(ATTACKER, amount='999999.00')  # attacker guesses a real receipt, inflates amount
        self.confirm(SAFARICOM)                       # the real one arrives
        c2b = C2BPayment.objects.get()
        self.assertFalse(c2b.held_for_review)
        self.assertEqual(c2b.amount, Decimal('7500'))  # Safaricom's details win
        self.assertEqual(self.lease.total_paid, Decimal('7500'))
        self.confirm(SAFARICOM)  # retry: still credited once
        self.assertEqual(Payment.objects.count(), 1)

    def test_landlord_can_assign_a_held_payment_after_checking(self):
        self.confirm(ATTACKER)
        c2b = C2BPayment.objects.get()
        self.client.force_login(self.owner)
        self.client.post(reverse('payments:assign_unmatched', args=[c2b.pk]), {f'c{c2b.pk}-lease': self.lease.pk})
        c2b.refresh_from_db()
        self.assertFalse(c2b.held_for_review)
        self.assertEqual(self.lease.total_paid, Decimal('7500'))

    def test_fake_stk_callback_is_ignored(self):
        txn = MpesaTransaction.objects.create(account=self.account, lease=self.lease, phone='254712345678',
                                              amount=7500, checkout_request_id='ws_CO_X')
        url = reverse('payments:stk_callback', args=[self.account.callback_token])
        body = json.dumps(success_callback('ws_CO_X', amount=7500, receipt='FAKE1'))
        self.client.post(url, body, content_type='application/json', REMOTE_ADDR=ATTACKER)
        txn.refresh_from_db()
        self.assertEqual(txn.status, 'pending')  # the status check will ask Safaricom
        self.assertFalse(Payment.objects.exists())
        self.client.post(url, body, content_type='application/json', REMOTE_ADDR=SAFARICOM)
        txn.refresh_from_db()
        self.assertEqual(txn.status, 'success')

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_behind_proxy_uses_real_ip_and_ignores_forged_header(self):
        payload = json.dumps(c2b_body(trans_id='P1', bill_ref=self.lease.account_number))
        # Caddy appends the real client; an attacker can prepend a Safaricom IP but not replace it.
        self.client.post(self.c2b_url, payload, content_type='application/json',
                         REMOTE_ADDR='172.18.0.5', HTTP_X_FORWARDED_FOR=f'{SAFARICOM}, {ATTACKER}')
        self.assertTrue(C2BPayment.objects.get(trans_id='P1').held_for_review)
        payload = json.dumps(c2b_body(trans_id='P2', bill_ref=self.lease.account_number))
        self.client.post(self.c2b_url, payload, content_type='application/json',
                         REMOTE_ADDR='172.18.0.5', HTTP_X_FORWARDED_FOR=SAFARICOM)
        self.assertFalse(C2BPayment.objects.get(trans_id='P2').held_for_review)

    def test_sandbox_accounts_and_any_are_not_checked(self):
        self.account.environment = 'sandbox'
        self.account.save()
        self.confirm(ATTACKER, trans_id='S1')
        self.assertFalse(C2BPayment.objects.get(trans_id='S1').held_for_review)
        self.account.environment = 'production'
        self.account.save()
        with self.settings(MPESA={**MPESA_SETTINGS, 'CALLBACK_IPS': ['any']}):
            self.confirm(ATTACKER, trans_id='S2')
        self.assertFalse(C2BPayment.objects.get(trans_id='S2').held_for_review)
