"""Starting without Daraja: Paybill/Till number only, M-Pesa statement uploads, and clearer Daraja setup."""

import io
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from accounts.utils import mark_verified
from notifications.models import SmsMessage
from notifications.utils import reminder_text
from rentals.tests import make_lease

from .. import mpesa
from ..models import C2BPayment, MpesaAccount, Payment
from ..utils import StatementError, check_statement, read_statement
from . import MPESA_SETTINGS, make_account, mock_daraja

HEADINGS = ['Receipt No.', 'Completion Time', 'Initiation Time', 'Details', 'Transaction Status', 'Paid In',
            'Withdrawn', 'Balance', 'Balance Confirmed', 'Reason Type', 'Other Party Info',
            'Linked Transaction ID', 'A/C No.']


def statement_csv(rows):
    """A statement laid out like the M-Pesa business portal's export: account details, then the table."""
    out = io.StringIO()
    out.write('Account Holder:,RIVERSIDE APARTMENTS\nShort Code:,4012345\n\n')
    out.write(','.join(HEADINGS) + '\n')
    for receipt, when, details, status, paid_in, party, acc in rows:
        out.write(f'{receipt},{when},{when},{details},{status},"{paid_in}",,,true,Pay Bill Online,{party},,{acc}\n')
    return SimpleUploadedFile('statement.csv', out.getvalue().encode())


class ReadStatementTests(TestCase):
    def test_reads_money_paid_in_and_skips_the_rest(self):
        rows = read_statement(statement_csv([
            ('SJK4H7Q2LP', '14-10-2026 09:12:33', 'Pay Bill from 254712345678 - JOHN DOE Acc. LEASE1', 'Completed',
             '15,000.00', '254712345678 - JOHN DOE', 'LEASE1'),
            ('SJK4H7Q2LQ', '14-10-2026 10:00:00', 'Pay Bill from 2547******321 - MARY W Acc. b2', 'Completed',
             '8000.00', '2547******321 - MARY W', ''),
            ('SJK4H7Q2LR', '14-10-2026 11:00:00', 'Business Pay Bill Charge', 'Completed', '', '', ''),
            ('SJK4H7Q2LS', '14-10-2026 12:00:00', 'Pay Bill from 254700000000 - X', 'Failed', '500.00', '', ''),
        ]))
        self.assertEqual([r['receipt'] for r in rows], ['SJK4H7Q2LP', 'SJK4H7Q2LQ'])
        self.assertEqual(rows[0]['amount'], '15000.00')
        self.assertEqual((rows[0]['phone'], rows[0]['name']), ('254712345678', 'JOHN DOE'))
        self.assertEqual(rows[1]['account'], 'b2')  # from Details when the A/C column is empty

    def test_reads_excel(self):
        import datetime

        book = Workbook()
        sheet = book.active
        sheet.append(['Organisation Name', 'Riverside'])
        sheet.append(HEADINGS)
        sheet.append(['SJK4H7Q2LP', datetime.datetime(2026, 10, 14, 9, 12), None, 'Pay Bill', 'Completed', 15000,
                      None, None, None, None, '254712345678 - JOHN DOE', None, 'LEASE1'])
        data = io.BytesIO()
        book.save(data)
        rows = read_statement(SimpleUploadedFile('statement.xlsx', data.getvalue()))
        self.assertEqual((rows[0]['receipt'], rows[0]['time'], rows[0]['amount']),
                         ('SJK4H7Q2LP', '2026-10-14 09:12:00', '15000'))

    def test_explains_files_it_cannot_read(self):
        for name, content, words in [
            ('statement.xls', b'x', 'Save As'),
            ('statement.pdf', b'%PDF', 'Excel or CSV'),
            ('tenants.csv', b'Unit,Rent\nA1,15000\n', "doesn't look like an M-Pesa statement"),
        ]:
            with self.assertRaisesMessage(StatementError, words):
                read_statement(SimpleUploadedFile(name, content))


class CheckStatementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('landlord')
        self.account = MpesaAccount.objects.create(owner=self.owner, shortcode='4012345')
        self.lease = make_lease(self.owner, label='A4')
        self.other = make_lease(User.objects.create_user('other'), label='Z9')

    def rows(self, *items):
        return [{'line': i, 'receipt': receipt, 'time': '14-10-2026 09:00:00', 'amount': '15000', 'account': acc,
                 'phone': phone, 'name': '', 'details': ''} for i, (receipt, acc, phone) in enumerate(items)]

    def test_matching(self):
        Payment.objects.create(lease=self.lease, amount=15000, method='mpesa', reference='SJKOLD0001')
        checked = check_statement(self.rows(
            ('SJKNEW0001', f'LEASE{self.lease.pk}', ''),     # account number
            ('SJKNEW0002', 'a-4', '2547******321'),           # house number (shown as such)
            ('SJKNEW0003', 'nothing', '2547******321'),       # to assign by hand
            ('SJKOLD0001', '', ''),                           # already recorded
            ('SJKNEW0001', '', ''),                           # same receipt twice in the file
            ('SJKNEW0004', f'LEASE{self.other.pk}', ''),      # another landlord's tenant: never matched
        ), self.account)
        self.assertEqual([c['state'] for c in checked],
                         ['match', 'match', 'unmatched', 'recorded', 'recorded', 'unmatched'])
        self.assertEqual(checked[0]['lease'], self.lease)
        self.assertTrue(checked[1]['by_house'])
        self.assertFalse(checked[0]['by_house'])


class StatementUploadPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.account = MpesaAccount.objects.create(owner=self.owner, shortcode='4012345')
        self.lease = make_lease(self.owner, label='A4')
        self.client.force_login(self.owner)
        self.url = reverse('payments:statement_upload')

    def upload(self, **extra):
        return self.client.post(self.url, {'action': 'preview', 'file': statement_csv([
            ('SJK4H7Q2LP', '14-10-2026 09:12:33', 'Pay Bill', 'Completed', '15,000.00',
             '254712345678 - JOHN DOE', f'LEASE{self.lease.pk}'),
            ('SJK4H7Q2LQ', '14-10-2026 10:00:00', 'Pay Bill', 'Completed', '2000.00', '2547******321 - MARY W', 'x'),
        ]), **extra})

    def test_preview_then_record(self):
        preview = self.upload()
        self.assertContains(preview, '1 payment matched to tenants')
        self.assertContains(preview, 'Record 2 payments')
        self.assertFalse(Payment.objects.exists())

        resp = self.client.post(self.url, {'action': 'confirm'})
        self.assertRedirects(resp, reverse('payments:unmatched_list'))
        payment = Payment.objects.get()
        self.assertEqual((payment.lease, payment.amount, payment.reference), (self.lease, Decimal('15000'), 'SJK4H7Q2LP'))
        unmatched = C2BPayment.objects.get(payment__isnull=True)
        self.assertEqual((unmatched.trans_id, unmatched.source), ('SJK4H7Q2LQ', 'statement'))
        self.assertFalse(SmsMessage.objects.exists())  # receipts are opt-in for statement uploads
        self.account.refresh_from_db()
        self.assertIsNotNone(self.account.statement_imported_at)

        # Uploading the same statement again records nothing twice.
        self.assertContains(self.upload(), 'Record 0 payments')

    def test_receipts_when_asked(self):
        self.upload(send_receipts='on')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, {'action': 'confirm'})
        self.assertTrue(SmsMessage.objects.filter(lease=self.lease).exists())

    def test_needs_a_pay_number_first(self):
        self.account.delete()
        self.assertRedirects(self.client.get(self.url), reverse('payments:mpesa_settings'))

    def test_bad_file_is_explained(self):
        resp = self.client.post(self.url, {'action': 'preview', 'file': SimpleUploadedFile('s.pdf', b'%PDF')})
        self.assertContains(resp, 'Excel or CSV')


@override_settings(MPESA=MPESA_SETTINGS)
class PayNumberOnlyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        mark_verified(self.owner)
        self.client.force_login(self.owner)
        self.url = reverse('payments:mpesa_settings')
        self.lease = make_lease(self.owner, label='A4')

    def save(self, **fields):
        data = {'action': 'save', 'environment': 'sandbox', 'payment_type': 'paybill', 'shortcode': '4012345',
                'till_number': '', 'store_number': '', 'consumer_key': '', 'consumer_secret': '', 'passkey': '',
                **fields}
        return self.client.post(self.url, data)

    def test_paybill_number_is_enough_to_start(self):
        self.assertRedirects(self.save(), self.url)
        account = MpesaAccount.objects.get()
        self.assertTrue(account.has_pay_number)
        self.assertFalse(account.is_complete())

        settings_page = self.client.get(self.url)
        self.assertContains(settings_page, "You're ready to collect rent")
        self.assertContains(self.client.get(reverse('rentals:dashboard')), 'Upload your M-Pesa statement')
        self.assertIn('Paybill 4012345', reminder_text(self.lease, Decimal('15000')))

        portal = self.client.get(reverse('rentals:tenant_portal', args=[self.lease.tenant.portal_token]))
        self.assertContains(portal, '4012345')
        self.assertNotContains(portal, 'Pay with M-Pesa')  # PIN prompts need Daraja
        lease_page = self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.assertContains(lease_page, 'Sending M-Pesa PIN prompts needs Daraja')

    def test_daraja_keys_are_all_or_nothing(self):
        resp = self.save(consumer_key='ck')
        self.assertContains(resp, 'Fill in all three Daraja keys')
        self.assertFalse(MpesaAccount.objects.exists())

    def test_till_needs_store_number_only_for_daraja(self):
        self.save(payment_type='till', shortcode='', till_number='5123456')
        account = MpesaAccount.objects.get()
        self.assertEqual((account.pay_number, account.shortcode), ('5123456', ''))
        resp = self.save(payment_type='till', till_number='5123456', consumer_key='ck', consumer_secret='cs',
                         passkey='pk')
        self.assertContains(resp, 'Head Office / Store number')
        self.save(payment_type='till', till_number='5123456', store_number='7654321', consumer_key='ck',
                  consumer_secret='cs', passkey='pk')
        account.refresh_from_db()
        self.assertEqual((account.shortcode, account.pay_number), ('7654321', '5123456'))
        self.assertTrue(account.is_complete())

    def test_paybill_number_required(self):
        resp = self.save(shortcode='')
        self.assertContains(resp, 'Enter your Paybill number')


@override_settings(MPESA=MPESA_SETTINGS)
class DarajaErrorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.account = make_account(get_user_model().objects.create_user('landlord'))

    def test_errors_say_what_to_do(self):
        self.assertIn('M-Pesa Express', mpesa.explain_daraja_error(
            '401.003.01', 'Error Occurred - Invalid Access Token - Invalid API call as no apiproduct match found'))
        self.assertIn('Go Live', mpesa.explain_daraja_error('500.001.1001', 'Merchant does not exist'))
        self.assertIn('Safaricom rejected the request: odd', mpesa.explain_daraja_error('', 'odd'))

    @mock.patch('payments.mpesa.requests')
    def test_rejected_keys_mention_sandbox_or_live(self, req):
        mock_daraja(req, {})
        req.get.return_value.status_code = 401
        with self.assertRaisesMessage(mpesa.MpesaError, 'keys from a Live app only work with "Live"'):
            mpesa.verify_credentials(self.account)

    @mock.patch('payments.mpesa.requests')
    def test_already_registered_counts_as_registered(self, req):
        mock_daraja(req, {'errorCode': '500.003.1001', 'errorMessage': 'URLs are already registered'}, status=500)
        mpesa.register_c2b_urls(self.account)
        self.account.refresh_from_db()
        self.assertIsNotNone(self.account.c2b_registered_at)
