"""Live payment alerts, monthly report cards, and the installable app."""

import datetime
import json
import time
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core import mail
from django.core.management import call_command
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.verification import mark_verified
from notifications.models import LandlordPreferences, ReportCardSent, SmsMessage
from notifications.report_cards import send_all
from ops.jobs import JOBS
from payments.models import Payment
from reports.summary import month_summary
from rentals.tests import make_lease

D = Decimal


class LiveTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner)
        self.lease.ensure_charge_for(datetime.date.today())
        self.other = make_lease(User.objects.create_user('bob'), label='Z1')
        self.client.force_login(self.owner)

    def live(self, since, fragments=False):
        return self.client.get(reverse('rentals:live'), {'since': since, **({'fragments': 1} if fragments else {})}).json()

    def test_page_carries_the_cursor(self):
        Payment.objects.create(lease=self.lease, amount=1, method='cash')
        page = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(page, f'data-cursor="{Payment.objects.get().pk}"')
        self.assertContains(page, 'js/live.js')

    def test_reports_only_my_new_payments(self):
        old = Payment.objects.create(lease=self.lease, amount=1, method='cash')
        new = Payment.objects.create(lease=self.lease, amount=D('18000'), method='mpesa', reference='SJK1')
        Payment.objects.create(lease=self.other, amount=D('999'), method='cash')
        voided = Payment.objects.create(lease=self.lease, amount=5, method='cash')
        voided.void(self.owner, 'typo')
        data = self.live(old.pk)
        self.assertEqual([p['id'] for p in data['payments']], [new.pk])
        self.assertEqual(data['payments'][0]['amount'], 'KES 18,000')
        self.assertEqual(data['payments'][0]['tenant'], self.lease.tenant.full_name)
        self.assertEqual(self.live(data['cursor'])['payments'], [])

    def test_fragments_show_fresh_numbers(self):
        Payment.objects.create(lease=self.lease, amount=D('15000'), method='cash')
        data = self.live(0, fragments=True)
        self.assertIn('KES 15,000', data['fragments']['kpis'])
        self.assertIn('Tenants in arrears', data['fragments']['arrears'])
        self.assertIn('Recent payments', data['fragments']['recent'])

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('rentals:live')).status_code, 302)

    @override_settings(SESSION_COOKIE_AGE=2)
    def test_polling_does_not_keep_an_idle_session_alive(self):
        self.client.logout()
        self.client.post(reverse('login'), {'username': 'landlord', 'password': 'pw'})
        for _ in range(3):  # the browser keeps polling while nobody touches it
            time.sleep(1)
            self.client.get(reverse('rentals:live'))
        resp = self.client.get(reverse('rentals:dashboard'))
        self.assertEqual(resp.status_code, 302)  # idle timeout still applies


@override_settings(SITE_URL='https://rent.example.com')
class ReportCardTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', email='grace@example.com', first_name='Grace')
        mark_verified(self.owner)
        self.lease = make_lease(self.owner, rent='10000')
        self.month = datetime.date.today().replace(day=1)
        self.lease.ensure_charge_for(self.month)
        Payment.objects.create(lease=self.lease, amount=D('6000'), method='cash')

    def test_summary_numbers(self):
        s = month_summary(self.owner, self.month)
        self.assertEqual((s['billed'], s['collected'], s['rate']), (D('10000'), D('6000'), 60))
        self.assertEqual((s['arrears'], s['owing_count'], s['occupied'], s['vacant']), (D('4000'), 1, 1, 0))
        self.assertEqual(s['top_owing'][0][0], self.lease)

    def test_sent_once_by_email_with_the_numbers(self):
        self.assertEqual(send_all(self.month), 1)
        self.assertEqual(send_all(self.month), 0)  # running again (or daily) doesn't resend
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn(f'{self.month:%B %Y}', email.subject)
        self.assertIn('KES 6000 of KES 10000 billed (60%)', email.body)
        self.assertIn(f'https://rent.example.com/reports/?month={self.month:%Y-%m}', email.body)
        self.assertIn('KES 6000', email.alternatives[0][0])
        self.assertTrue(ReportCardSent.objects.get(owner=self.owner, month=self.month).email_sent)

    def test_sms_when_turned_on(self):
        LandlordPreferences.objects.create(user=self.owner, phone='254700000001', report_sms=True, report_email=False)
        send_all(self.month)
        self.assertEqual(len(mail.outbox), 0)
        sms = SmsMessage.objects.get(kind='report')
        self.assertIn('collected KES 6,000 of 10,000 (60%)', sms.body)
        self.assertEqual(sms.phone, '254700000001')

    def test_unverified_email_and_quiet_months_are_skipped(self):
        self.owner.profile.email_verified_at = None
        self.owner.profile.save()
        send_all(self.month)
        self.assertEqual(len(mail.outbox), 0)
        quiet = get_user_model().objects.create_user('quiet', email='q@example.com')
        make_lease(quiet, label='Q1')  # a property, but nothing billed or paid that month
        send_all(self.month)
        self.assertFalse(ReportCardSent.objects.filter(owner=quiet).exists())

    def test_preferences_page_and_preview(self):
        self.client.force_login(self.owner)
        url = reverse('notifications:preferences')
        resp = self.client.post(url, {'phone': '', 'report_email': 'on', 'report_sms': 'on'})
        self.assertContains(resp, 'Add your phone number')
        self.client.post(url, {'phone': '0712 000 111', 'report_email': 'on'})
        self.assertEqual(LandlordPreferences.objects.get(user=self.owner).phone, '254712000111')
        # The preview reports on last month, which has no activity in this test: nothing is sent or recorded.
        resp = self.client.post(url, {'action': 'preview'}, follow=True)
        self.assertContains(resp, 'Nothing to report for last month')
        self.assertFalse(ReportCardSent.objects.exists())

    def test_scheduled_daily(self):
        job = next(j for j in JOBS if j.name == 'send_report_cards')
        self.assertEqual(job.daily_at, datetime.time(7, 0))
        call_command('send_report_cards', month=f'{self.month:%Y-%m}', stdout=open('/dev/null', 'w'))
        self.assertTrue(ReportCardSent.objects.filter(owner=self.owner).exists())


class PwaTests(TestCase):
    def test_landlord_manifest(self):
        resp = self.client.get('/manifest.webmanifest')
        self.assertEqual(resp['Content-Type'], 'application/manifest+json')
        data = json.loads(resp.content)
        self.assertEqual((data['name'], data['display'], data['scope']), ('Liskodi', 'standalone', '/'))
        self.assertTrue(data['start_url'].startswith('/dashboard/'))
        self.assertIn('maskable', [icon.get('purpose') for icon in data['icons']])
        for icon in data['icons']:
            self.assertIsNotNone(finders.find(icon['src'].removeprefix('/static/')), icon['src'])

    def test_tenant_manifest_opens_their_own_page(self):
        lease = make_lease(get_user_model().objects.create_user('landlord'))
        token = lease.tenant.portal_token
        page = self.client.get(reverse('rentals:tenant_portal', args=[token]))
        self.assertContains(page, f'href="/t/{token}/manifest.webmanifest"')
        data = json.loads(self.client.get(f'/t/{token}/manifest.webmanifest').content)
        self.assertEqual((data['start_url'], data['scope']), (f'/t/{token}/', f'/t/{token}/'))
        self.assertEqual(self.client.get('/t/nope/manifest.webmanifest').status_code, 404)

    def test_service_worker_caches_no_pages(self):
        resp = self.client.get('/sw.js')
        self.assertEqual(resp['Content-Type'], 'application/javascript')
        self.assertIn('no-cache', resp['Cache-Control'])
        body = resp.content.decode()
        self.assertIn("'/offline/'", body)
        self.assertIn('/static/css/app.css', body)
        self.assertIn("request.mode === 'navigate'", body)
        self.assertNotIn('cache.put(request', body.split("request.mode === 'navigate'")[1].split('return;')[0])

    def test_unhashed_static_files_are_never_served_stale(self):
        # Development (DEBUG): file names don't change with their contents, so the worker must ask the
        # server first, and its version must change when a file does (so old caches are cleared).
        storages = {**settings.STORAGES,
                    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
        with override_settings(STORAGES=storages):
            body = self.client.get('/sw.js').content.decode()
            self.assertIn('fetch(request).then(save).catch(() => caches.match(request))', body)
            self.assertNotIn('hit || fetch', body)
            with mock.patch('config.pwa._contents_fingerprint', return_value='changed'):
                self.assertNotEqual(self.client.get('/sw.js').content.decode().split('\n')[3], body.split('\n')[3])

    def test_offline_page_and_head_tags(self):
        self.assertContains(self.client.get('/offline/'), "You're offline")
        home = self.client.get(reverse('rentals:home'))
        self.assertContains(home, '<link rel="manifest" href="/manifest.webmanifest">')
        self.assertContains(home, '<meta name="theme-color" content="#0e7a4b">')
