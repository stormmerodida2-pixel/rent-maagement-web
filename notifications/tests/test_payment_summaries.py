"""Evening WhatsApp summaries for landlords, only on days they were paid."""

import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import StaffMember
from payments.models import Payment
from rentals.tests import make_lease

from ..models import LandlordPreferences, SmsMessage, WhatsAppSettings
from ..utils import payment_summary, send_payment_summaries, summary_text

TODAY = timezone.localdate()


class SummaryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('landlord', first_name='Grace')
        prefs = LandlordPreferences.for_user(self.owner)
        prefs.phone = '254712345678'
        prefs.save()
        self.a = make_lease(self.owner, label='A1', start=TODAY.replace(day=1))
        self.b = make_lease(self.owner, label='A2', start=TODAY.replace(day=1))
        self.a.ensure_charge_for(TODAY)
        self.b.ensure_charge_for(TODAY)
        self.peter = User.objects.create_user('peter', first_name='Peter')
        StaffMember.objects.create(owner=self.owner, user=self.peter)

    def pay(self, lease, amount, method='mpesa', by=None, when=None):
        return Payment.objects.create(lease=lease, amount=Decimal(amount), method=method, recorded_by=by,
                                      paid_at=when or timezone.now(), reference=f'R{Payment.objects.count()}')

    def test_what_it_says(self):
        self.pay(self.a, 10000)
        self.pay(self.a, 5000, method='cash', by=self.peter)
        self.pay(self.b, 2000, method='cash', by=self.owner)
        day, total, how, still = payment_summary(self.owner, TODAY)
        self.assertEqual(total, 'KES 17,000')
        self.assertEqual(how, '1 M-Pesa, 1 cash by Peter, 1 cash by you')
        # A2: 15,000 rent − 2,000; and Peter hasn't handed over the cash he took yet.
        self.assertEqual(still, '1 tenant still owes KES 13,000. Peter is holding KES 5,000 cash.')
        text = summary_text([day, total, how, still])
        self.assertIn('KES 17,000 received (1 M-Pesa, 1 cash by Peter, 1 cash by you)', text)

    def test_nothing_on_days_without_payments(self):
        self.pay(self.a, 10000, when=timezone.now() - datetime.timedelta(days=1))
        self.assertIsNone(payment_summary(self.owner, TODAY))
        send_payment_summaries(TODAY)
        self.assertFalse(SmsMessage.objects.exists())

    def test_sent_once_a_day_by_sms_until_whatsapp_is_set_up(self):
        self.pay(self.a, 15000)
        self.pay(self.b, 15000)
        self.assertEqual(send_payment_summaries(TODAY)['sms'], 1)
        send_payment_summaries(TODAY)  # the job ran twice: still one message
        sms = SmsMessage.objects.get(phone='254712345678')
        self.assertIn('KES 30,000 received (2 M-Pesa). Everyone is paid up.', sms.body)

    @mock.patch('notifications.utils.requests.post')
    def test_whatsapp_template_when_set_up(self, post):
        WhatsAppSettings.objects.update_or_create(pk=1, defaults={'phone_number_id': '123', 'access_token': 'tok'})
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'messages': [{'id': 'wamid.1'}]}
        self.pay(self.a, 15000)
        self.assertEqual(send_payment_summaries(TODAY)['whatsapp'], 1)
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['to'], '254712345678')
        self.assertEqual(payload['template']['name'], 'payments_summary')
        params = [p['text'] for p in payload['template']['components'][0]['parameters']]
        self.assertEqual(params[1:], ['KES 15,000', '1 M-Pesa', '1 tenant still owes KES 15,000.'])
        self.assertIn('/123/messages', post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer tok')
        self.assertFalse(SmsMessage.objects.exists())

    @mock.patch('notifications.utils.requests.post')
    def test_whatsapp_error_falls_back_to_sms(self, post):
        WhatsAppSettings.objects.update_or_create(pk=1, defaults={'phone_number_id': '123', 'access_token': 'tok'})
        post.return_value.status_code = 400
        post.return_value.json.return_value = {'error': {'message': 'Template not approved'}}
        self.pay(self.a, 15000)
        self.assertEqual(send_payment_summaries(TODAY)['failed'], 1)
        self.assertTrue(SmsMessage.objects.filter(phone='254712345678').exists())

    def test_landlords_can_turn_it_off(self):
        prefs = LandlordPreferences.for_user(self.owner)
        prefs.payment_summary = False
        prefs.save()
        self.pay(self.a, 15000)
        send_payment_summaries(TODAY)
        self.assertFalse(SmsMessage.objects.exists())

    def test_staff_cash_records_who_collected_it(self):
        self.client.force_login(self.peter)
        self.client.post(reverse('staff:record_cash', args=[self.a.pk]),
                         {'amount': '5000', 'paid_at': timezone.localtime().strftime('%Y-%m-%dT%H:%M'),
                          'reference': '', 'notes': ''})
        self.assertEqual(Payment.objects.get().recorded_by, self.peter)


class WhatsAppSettingsPageTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('admin', 'admin@example.com', 'pw')
        self.url = reverse('notifications:whatsapp')

    def test_administrator_sets_it_up_and_tests_it(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.url), 'Sending by SMS')
        self.client.post(self.url, {'phone_number_id': '123', 'access_token': 'secret-token',
                                    'template_name': 'payments_summary', 'template_language': 'en'})
        page = self.client.get(self.url)
        self.assertContains(page, 'Set up')
        self.assertNotContains(page, 'secret-token')
        self.client.post(self.url, {'phone_number_id': '123', 'access_token': '',
                                    'template_name': 'payments_summary', 'template_language': 'en'})
        self.assertEqual(WhatsAppSettings.load().access_token, 'secret-token')  # blank keeps it
        with mock.patch('notifications.utils.requests.post') as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {}
            resp = self.client.post(self.url, {'action': 'test', 'test_phone': '0712345678'}, follow=True)
        self.assertContains(resp, 'Sent a sample summary to 254712345678')

    def test_landlords_cannot_see_it(self):
        self.client.force_login(get_user_model().objects.create_user('landlord'))
        self.assertEqual(self.client.get(self.url).status_code, 404)
