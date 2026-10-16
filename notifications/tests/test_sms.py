import datetime
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from payments.tests import make_account
from rentals.tests import make_lease

from ..models import SmsMessage
from ..utils import send_rent_reminder, send_sms

AT_SETTINGS = {
    'BACKEND': 'africastalking', 'AT_USERNAME': 'sandbox', 'AT_API_KEY': 'k',
    'AT_SENDER_ID': '', 'PAYMENT_RECEIPTS': True,
}


class SendSmsTests(TestCase):
    def test_console_backend_logs(self):
        msg = send_sms('0712345678', 'hello')
        self.assertEqual((msg.status, msg.phone), (SmsMessage.Status.LOGGED, '254712345678'))

    def test_invalid_number_is_recorded_not_raised(self):
        msg = send_sms('123', 'hello')
        self.assertEqual(msg.status, SmsMessage.Status.FAILED)
        self.assertIn('not a valid', msg.error)

    @override_settings(SMS=AT_SETTINGS)
    @mock.patch('notifications.utils.requests.post')
    def test_africastalking(self, post):
        post.return_value.json.return_value = {'SMSMessageData': {'Message': 'Sent to 1/1', 'Recipients': [
            {'statusCode': 101, 'number': '+254712345678', 'status': 'Success', 'cost': 'KES 0.8000', 'messageId': 'ATPid_1'},
        ]}}
        msg = send_sms('0712345678', 'hello')
        self.assertEqual((msg.status, msg.provider_message_id), (SmsMessage.Status.SENT, 'ATPid_1'))
        self.assertEqual(post.call_args.args[0], 'https://api.sandbox.africastalking.com/version1/messaging')
        self.assertEqual(post.call_args.kwargs['data']['to'], '+254712345678')

    @override_settings(SMS=AT_SETTINGS)
    @mock.patch('notifications.utils.requests.post')
    def test_africastalking_rejection(self, post):
        post.return_value.json.return_value = {'SMSMessageData': {'Message': 'Sent to 0/1', 'Recipients': [
            {'statusCode': 403, 'number': '+254712345678', 'status': 'InvalidPhoneNumber', 'cost': '0', 'messageId': 'None'},
        ]}}
        msg = send_sms('0712345678', 'hello')
        self.assertEqual((msg.status, msg.error), (SmsMessage.Status.FAILED, 'InvalidPhoneNumber'))


class ReminderTests(TestCase):
    def setUp(self):
        owner = get_user_model().objects.create_user('landlord')
        self.owing = make_lease(owner, label='A1', due_day=5)
        self.owing.ensure_charge_for(datetime.date(2026, 1, 1))
        self.paid_up = make_lease(owner, label='A2')

    def test_reminder_without_mpesa_only_links_portal(self):
        msg = send_rent_reminder(self.owing)
        self.assertNotIn('Paybill', msg.body)
        self.assertIn(f'Pay online: http://localhost:8000/t/{self.owing.tenant.portal_token}/', msg.body)

    def test_till_reminder_has_no_account_number(self):
        make_account(self.owing.unit.property.owner, payment_type='till', till_number='555111')
        msg = send_rent_reminder(self.owing)
        self.assertIn('Till 555111', msg.body)
        self.assertNotIn('account LEASE', msg.body)

    def test_reminder_text(self):
        make_account(self.owing.unit.property.owner)
        msg = send_rent_reminder(self.owing)
        self.assertIn('Paybill 600986', msg.body)
        self.assertIn('KES 15,000', msg.body)
        self.assertIn('overdue', msg.body)
        self.assertIn(self.owing.account_number, msg.body)
        self.assertIn(f'/t/{self.owing.tenant.portal_token}/', msg.body)

    def test_command_only_reminds_owing_tenants_and_respects_gap(self):
        call_command('send_rent_reminders', stdout=StringIO())
        call_command('send_rent_reminders', stdout=StringIO())  # within 3 days -> skipped
        self.assertEqual(list(SmsMessage.objects.values_list('lease', flat=True)), [self.owing.pk])
        call_command('send_rent_reminders', min_days_between=0, stdout=StringIO())
        self.assertEqual(SmsMessage.objects.count(), 2)

    def test_dry_run_sends_nothing(self):
        out = StringIO()
        call_command('send_rent_reminders', dry_run=True, stdout=out)
        self.assertIn('254712345678', out.getvalue())
        self.assertFalse(SmsMessage.objects.exists())
