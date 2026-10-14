"""The rent-day ring on the dashboard, and "Remind everyone who owes"."""

import datetime
from decimal import Decimal
from unittest import mock

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase, override_settings
from django.urls import reverse

from audit.models import AuditEntry
from notifications.models import SmsMessage
from notifications.utils import send_personal_sms
from payments.models import Payment

from . import make_lease

D = Decimal


class RentDayTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('landlord', password='pw')
        today = datetime.date.today()
        self.paid = make_lease(self.owner, label='A1', rent='10000', start=today.replace(day=1))
        self.owes = make_lease(self.owner, label='A2', rent='10000', start=today.replace(day=1))
        self.owes2 = make_lease(self.owner, label='A3', rent='20000', start=today.replace(day=1))
        for lease in (self.paid, self.owes, self.owes2):
            lease.ensure_charge_for(today)
        Payment.objects.create(lease=self.paid, amount=D('10000'), method='cash')
        self.stranger = make_lease(User.objects.create_user('other'), label='Z9')
        self.stranger.ensure_charge_for(today)
        self.client.force_login(self.owner)

    # --- Ring ----------------------------------------------------------------------------

    def test_ring_shows_tenants_paid_and_share_collected(self):
        page = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(page, '1 of 3 tenants paid up')
        self.assertContains(page, '25%')  # 10,000 of 40,000
        self.assertContains(page, 'stroke-dashoffset="75"')
        self.assertContains(page, '2 still to pay')

    def test_ring_celebrates_when_everyone_has_paid(self):
        Payment.objects.create(lease=self.owes, amount=D('10000'), method='cash')
        Payment.objects.create(lease=self.owes2, amount=D('20000'), method='cash')
        page = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(page, 'Everyone has paid')
        self.assertContains(page, 'stroke-dashoffset="0"')
        self.assertNotContains(page, 'Remind everyone who owes')

    def test_ring_updates_live(self):
        data = self.client.get(reverse('rentals:live'), {'since': 0, 'fragments': 1}).json()
        self.assertIn('1 of 3 tenants paid up', data['fragments']['kpis'])

    # --- Remind everyone -----------------------------------------------------------------

    def test_preview_lists_only_my_tenants_who_owe(self):
        page = self.client.get(reverse('rentals:remind_all'))
        self.assertContains(page, 'Tenant A2')
        self.assertContains(page, 'Tenant A3')
        self.assertNotContains(page, 'Tenant A1')  # paid up
        self.assertNotContains(page, 'Tenant Z9')  # another landlord's
        self.assertContains(page, 'your rent balance for A3 is KES 20,000')
        self.assertContains(page, '<span data-remind-count>2</span>')

    def test_tenants_reminded_today_are_unticked(self):
        SmsMessage.objects.create(lease=self.owes, phone='254712345678', body='x', kind='reminder', status='sent')
        page = self.client.get(reverse('rentals:remind_all'))
        self.assertContains(page, 'Reminded')
        self.assertContains(page, '<span data-remind-count>1</span>')
        self.assertContains(page, f'value="{self.owes2.pk}" id="remind-{self.owes2.pk}" data-parts="1"\n                     checked')

    def test_sends_one_personal_sms_to_each_chosen_tenant(self):
        resp = self.client.post(reverse('rentals:remind_all'),
                                {'lease': [self.owes.pk, self.owes2.pk, self.paid.pk, self.stranger.pk]}, follow=True)
        self.assertContains(resp, 'Sent 2 reminders.')
        sms = SmsMessage.objects.filter(kind='reminder').order_by('lease__unit__label')
        self.assertEqual([m.lease for m in sms], [self.owes, self.owes2])
        self.assertIn('KES 10,000', sms[0].body)
        self.assertIn('KES 20,000', sms[1].body)
        self.assertTrue(AuditEntry.objects.filter(action='reminders.sent', owner=self.owner).exists())

    def test_nothing_chosen(self):
        resp = self.client.post(reverse('rentals:remind_all'), {}, follow=True)
        self.assertContains(resp, 'Choose at least one tenant')
        self.assertFalse(SmsMessage.objects.exists())

    def test_login_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('rentals:remind_all')).status_code, 302)


@override_settings(SMS={**settings.SMS, 'BACKEND': 'africastalking',
                        'AT_USERNAME': 'sandbox', 'AT_API_KEY': 'k', 'AT_SENDER_ID': ''})
class PersonalSmsTests(TestCase):
    def test_failures_are_recorded_not_raised(self):
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {'SMSMessageData': {'Recipients': [{'statusCode': 101, 'messageId': 'm1'}]}}

        def post(url, data, **kwargs):
            if data['to'] == '+254700000002':
                raise requests.ConnectionError('network down')
            return ok

        with mock.patch('notifications.utils.requests.post', side_effect=post):
            msgs = send_personal_sms([(None, '0700000001', 'one'), (None, '0700000002', 'two')])
        self.assertEqual([m.status for m in msgs], ['sent', 'failed'])
        self.assertEqual(SmsMessage.objects.count(), 2)
        self.assertIn('network down', SmsMessage.objects.get(body='two').error)


class ToastTests(TestCase):
    def test_toast_wording_and_chime(self):
        js = open(finders.find('js/live.js'), encoding='utf-8').read()
        self.assertIn("' just paid '", js)
        self.assertIn('createOscillator', js)
        self.assertIn("localStorage.getItem('payment-chime')", js)
