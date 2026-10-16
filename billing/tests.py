"""Billing landlords: trials, monthly bills, paying (Till code or M-Pesa prompt), read-only when unpaid."""

import datetime
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import StaffMember
from accounts.utils import mark_verified
from notifications.models import SmsMessage
from payments.tests import mock_daraja
from rentals.models import Property
from rentals.tests import make_lease

from .models import BillingAccount, Invoice
from .utils import account_for, make_invoice, mark_paid, run_billing, status

TODAY = timezone.localdate()
DAY = datetime.timedelta(days=1)
ON = {**settings.BILLING, 'ENABLED': True, 'TILL': '9505957'}
PROMPTS = {**ON, 'MPESA_ENVIRONMENT': 'sandbox', 'MPESA_STORE_NUMBER': '7654321', 'MPESA_CONSUMER_KEY': 'ck',
           'MPESA_CONSUMER_SECRET': 'cs', 'MPESA_PASSKEY': 'pk', 'CALLBACK_TOKEN': 'secret-token'}


def landlord(username='landlord', units=8):
    owner = get_user_model().objects.create_user(username, password='pw', email=f'{username}@example.com')
    for i in range(units):
        make_lease(owner, label=f'U{i}')
    return owner


@override_settings(BILLING=ON)
class BillTests(TestCase):
    def setUp(self):
        self.owner = landlord()
        self.account = account_for(self.owner, TODAY)

    def test_trial_then_bill_three_days_before(self):
        self.assertEqual(self.account.trial_ends, TODAY + datetime.timedelta(days=30))
        self.assertEqual(status(self.account, TODAY), 'trial')
        self.assertIsNone(make_invoice(self.account, self.account.paid_until - 4 * DAY))
        invoice = make_invoice(self.account, self.account.paid_until - 3 * DAY)
        self.assertEqual((invoice.units, invoice.billed_units, invoice.amount), (8, 3, 225))  # 5 free, KES 75 each
        self.assertEqual(invoice.due_date, self.account.paid_until)
        self.assertIn('KES 225', mail.outbox[-1].subject)
        self.assertIn('Till 9505957', mail.outbox[-1].body)
        self.assertIsNone(make_invoice(self.account, self.account.paid_until - 2 * DAY))  # one bill per month

    def test_small_landlords_pay_nothing(self):
        small = landlord('small', units=5)
        account = account_for(small, TODAY)
        invoice = make_invoice(account, account.paid_until)
        self.assertEqual((invoice.amount, invoice.status, invoice.method), (0, 'paid', 'free'))
        account.refresh_from_db()
        self.assertEqual(account.paid_until, invoice.period_end)
        self.assertNotIn('small@example.com', [m.to[0] for m in mail.outbox])

    def test_overdue_then_read_only_then_paid(self):
        due = self.account.paid_until
        invoice = make_invoice(self.account, due)
        self.assertEqual(status(self.account, due), 'overdue')
        self.assertEqual(status(self.account, due + 7 * DAY), 'locked')
        mark_paid(invoice, Invoice.Method.TILL, 'SJK4H7Q2LP')
        self.account.refresh_from_db()
        self.assertEqual(self.account.paid_until, invoice.period_end)
        self.assertEqual(status(self.account, due + 7 * DAY), 'active')
        self.assertIn('Payment received', mail.outbox[-1].subject)

    def test_months_keep_their_day(self):
        self.account.paid_until = datetime.date(2027, 1, 31)
        self.account.save()
        invoice = make_invoice(self.account, datetime.date(2027, 1, 31))
        self.assertEqual(invoice.period_end, datetime.date(2027, 2, 28))

    def test_daily_job_sends_each_notice_once(self):
        due = self.account.paid_until
        run_billing(due - 3 * DAY)
        run_billing(due + DAY)
        run_billing(due + 2 * DAY)
        run_billing(due + 8 * DAY)
        subjects = [m.subject for m in mail.outbox]
        self.assertEqual(sum('bill:' in s for s in subjects), 1)
        self.assertEqual(sum('overdue' in s for s in subjects), 1)
        self.assertEqual(sum('read-only' in s for s in subjects), 1)


class SwitchedOffTests(TestCase):
    def test_nothing_happens_while_billing_is_off(self):
        owner = landlord()
        self.client.force_login(owner)
        self.assertEqual(run_billing(TODAY + datetime.timedelta(days=400)), {'bills': 0, 'overdue': 0, 'locked': 0})
        self.assertFalse(BillingAccount.objects.exists())
        self.assertEqual(self.client.get(reverse('billing:billing')).status_code, 404)
        self.assertNotContains(self.client.get(reverse('accounts:settings')), 'Your Rentipay plan')


@override_settings(BILLING=ON)
class ReadOnlyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = landlord(units=6)
        mark_verified(self.owner)
        account = account_for(self.owner)
        account.paid_until = account.trial_ends = TODAY - 10 * DAY  # unpaid for 10 days
        account.save()
        self.invoice = make_invoice(account, TODAY)
        self.client.force_login(self.owner)

    def test_can_look_but_not_change(self):
        self.assertEqual(self.client.get(reverse('rentals:dashboard')).status_code, 200)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), 'read-only')
        resp = self.client.post(reverse('rentals:property_create'), {'name': 'New', 'address': '', 'water_rate': ''})
        self.assertRedirects(resp, reverse('billing:billing'))
        self.assertEqual(Property.objects.filter(name='New').count(), 0)

    def test_tenants_are_not_affected(self):
        self.client.logout()
        lease = self.owner.properties.first().units.first().leases.first()
        page = self.client.get(reverse('rentals:tenant_portal', args=[lease.tenant.portal_token]))
        self.assertEqual(page.status_code, 200)

    def test_rent_reminders_pause(self):
        call_command('send_rent_reminders', stdout=StringIO(), stderr=StringIO())
        self.assertFalse(SmsMessage.objects.filter(kind=SmsMessage.Kind.REMINDER).exists())

    def test_staff_are_read_only_too(self):
        User = get_user_model()
        member = StaffMember.objects.create(owner=self.owner, user=User.objects.create_user('caretaker', password='pw'))
        self.client.force_login(member.user)
        lease = self.owner.properties.first().units.first().leases.first()
        resp = self.client.post(reverse('staff:record_cash', args=[lease.pk]), {'amount': '100'})
        self.assertRedirects(resp, reverse('staff:home'))

    def test_till_code_unlocks_while_it_is_checked(self):
        self.assertContains(self.client.post(reverse('billing:claim'), {'code': 'not a code'}, follow=True),
                            '10-character code')
        self.client.post(reverse('billing:claim'), {'code': 'sjk4h7q2lp'})
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.claimed_reference, 'SJK4H7Q2LP')
        self.assertEqual(status(self.invoice.account), 'checking')
        resp = self.client.post(reverse('rentals:property_create'), {'name': 'New', 'address': '', 'water_rate': ''})
        self.assertEqual(Property.objects.filter(name='New').count(), 1, resp)

    def test_used_code_is_refused(self):
        other = landlord('other', units=6)
        other_invoice = make_invoice(account_for(other), account_for(other).paid_until)
        mark_paid(other_invoice, Invoice.Method.TILL, 'SJK4H7Q2LP')
        resp = self.client.post(reverse('billing:claim'), {'code': 'SJK4H7Q2LP'}, follow=True)
        self.assertContains(resp, 'already been used')


@override_settings(BILLING=ON)
class OverviewTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('admin', 'admin@example.com', 'pw')
        self.owner = landlord()
        account = account_for(self.owner)
        self.invoice = make_invoice(account, account.paid_until)
        self.invoice.claimed_reference, self.invoice.claimed_at = 'SJK4H7Q2LP', timezone.now()
        self.invoice.save()
        self.client.force_login(self.admin)

    def test_confirm_a_till_payment(self):
        page = self.client.get(reverse('billing:overview'))
        self.assertContains(page, 'SJK4H7Q2LP')
        self.client.post(reverse('billing:mark_paid', args=[self.invoice.pk]))
        self.invoice.refresh_from_db()
        self.assertEqual((self.invoice.status, self.invoice.method, self.invoice.reference), ('paid', 'till', 'SJK4H7Q2LP'))
        self.assertEqual(mail.outbox[-1].to, ['landlord@example.com'])

    def test_reject_and_extend(self):
        self.client.post(reverse('billing:reject_claim', args=[self.invoice.pk]))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.claimed_reference, '')
        before = self.invoice.account.paid_until
        self.client.post(reverse('billing:extend', args=[self.owner.pk]), {'days': '30'})
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'void')
        self.assertEqual(self.invoice.account.paid_until, max(before, TODAY) + 30 * DAY)

    def test_landlords_cannot_see_it(self):
        self.client.force_login(self.owner)
        self.assertNotEqual(self.client.get(reverse('billing:overview')).status_code, 200)
        self.assertEqual(self.client.get(reverse('billing:invoice', args=[self.invoice.pk])).status_code, 200)
        self.client.force_login(landlord('nosy'))
        self.assertEqual(self.client.get(reverse('billing:invoice', args=[self.invoice.pk])).status_code, 404)


@override_settings(BILLING=PROMPTS, MPESA={**settings.MPESA, 'CALLBACK_BASE_URL': 'https://example.ngrok.app'})
class PromptTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = landlord()
        account = account_for(self.owner)
        self.invoice = make_invoice(account, account.paid_until)
        self.client.force_login(self.owner)

    @mock.patch('payments.mpesa.requests')
    def test_pay_with_a_prompt(self, req):
        mock_daraja(req, {'ResponseCode': '0', 'CheckoutRequestID': 'ws_CO_1', 'MerchantRequestID': 'm1'})
        self.client.post(reverse('billing:pay'), {'phone': '0712345678'})
        payload = req.post.call_args.kwargs['json']
        self.assertEqual((payload['PartyB'], payload['Amount'], payload['TransactionType']),
                         ('9505957', 225, 'CustomerBuyGoodsOnline'))
        self.assertIn('/billing/hooks/secret-token/prompt/', payload['CallBackURL'])

        callback = {'Body': {'stkCallback': {'CheckoutRequestID': 'ws_CO_1', 'ResultCode': 0, 'CallbackMetadata': {
            'Item': [{'Name': 'Amount', 'Value': 225}, {'Name': 'MpesaReceiptNumber', 'Value': 'SJK4H7Q2LP'}]}}}}
        self.assertEqual(self.client.post(reverse('billing:stk_callback', args=['wrong']), callback,
                                          content_type='application/json').status_code, 404)
        self.client.post(reverse('billing:stk_callback', args=['secret-token']), callback,
                         content_type='application/json')
        self.invoice.refresh_from_db()
        self.assertEqual((self.invoice.status, self.invoice.method, self.invoice.reference), ('paid', 'prompt', 'SJK4H7Q2LP'))

    def test_short_payment_is_not_accepted(self):
        Invoice.objects.filter(pk=self.invoice.pk).update(checkout_request_id='ws_CO_2')
        callback = {'Body': {'stkCallback': {'CheckoutRequestID': 'ws_CO_2', 'ResultCode': 0, 'CallbackMetadata': {
            'Item': [{'Name': 'Amount', 'Value': 1}, {'Name': 'MpesaReceiptNumber', 'Value': 'SJK4H7Q2LQ'}]}}}}
        self.client.post(reverse('billing:stk_callback', args=['secret-token']), callback, content_type='application/json')
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'open')

    @override_settings(BILLING={**PROMPTS, 'MPESA_ENVIRONMENT': 'production'},
                       MPESA={**settings.MPESA, 'CALLBACK_IPS': ['196.201.214.0/24']})
    def test_callbacks_from_elsewhere_are_refused(self):
        resp = self.client.post(reverse('billing:stk_callback', args=['secret-token']), {},
                                content_type='application/json', REMOTE_ADDR='203.0.113.9')
        self.assertEqual(resp.status_code, 403)
