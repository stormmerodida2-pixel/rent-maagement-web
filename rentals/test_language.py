"""Tenants who read Kiswahili get their SMS, rent page, receipts and statements in Kiswahili."""

import datetime
import gettext
import re
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from announcements.models import Announcement
from announcements.sending import send as send_announcement
from notifications.models import SmsMessage
from notifications.sms import receipt_text, reminder_text
from payments.models import Payment
from support import notify
from support.models import Complaint

from .models import RentCharge, Tenant
from .tests import make_lease

D = Decimal


class LanguageTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('landlord', password='pw')
        self.sw = make_lease(self.owner, label='A1')
        self.sw.tenant.language = 'sw'
        self.sw.tenant.full_name = 'Wanjiku Kamau'
        self.sw.tenant.save()
        self.en = make_lease(self.owner, label='B1')
        self.sw.ensure_charge_for(datetime.date(2026, 10, 1))

    def portal(self, lease, name='rentals:tenant_portal', *args):
        return self.client.get(reverse(name, args=[lease.tenant.portal_token, *args]))

    # --- SMS -----------------------------------------------------------------------------

    def test_reminder_in_kiswahili_only_for_kiswahili_readers(self):
        text = reminder_text(self.sw, D('15000'))
        self.assertTrue(text.startswith('Habari Wanjiku, salio la kodi yako ya A1 ni KES 15,000'), text)
        self.assertIn('Lipa mtandaoni', text)
        self.assertNotIn('your rent', text)
        self.assertIn('your rent balance for B1 is KES 15,000', reminder_text(self.en, D('15000')))

    def test_receipt_in_kiswahili(self):
        payment = Payment.objects.create(lease=self.sw, amount=D('5000'), method='mpesa', reference='SJK1')
        self.assertEqual(receipt_text(payment, D('10000')),
                         'Tumepokea KES 5,000 (M-Pesa SJK1) kwa A1. Asante! Salio: KES 10,000.')
        self.assertIn('Umelipa kodi yote.', receipt_text(payment, D('0')))

    def test_reply_alert_in_kiswahili(self):
        complaint = Complaint.objects.create(owner=self.owner, tenant=self.sw.tenant, lease=self.sw, subject='Maji')
        msg = notify.tenant_reply(complaint)
        self.assertTrue(msg.body.startswith('Mwenye nyumba amejibu "Maji". Soma hapa:'), msg.body)

    # --- Pages ---------------------------------------------------------------------------

    def test_rent_page_in_tenants_language(self):
        page = self.portal(self.sw)
        self.assertContains(page, '<html lang="sw">')
        self.assertEqual(page['Content-Language'], 'sw')
        self.assertContains(page, 'Habari Wanjiku')
        self.assertContains(page, 'Ripoti tatizo')
        self.assertContains(page, 'Nini kinahitaji kurekebishwa?')  # form labels too
        self.assertNotContains(page, 'Report a problem')
        english = self.portal(self.en)
        self.assertContains(english, 'Report a problem')
        self.assertContains(english, '<html lang="en')

    def test_messages_after_actions_are_translated(self):
        resp = self.client.post(reverse('rentals:tenant_portal', args=[self.sw.tenant.portal_token]),
                                {'action': 'maintenance', 'unit': self.sw.unit.pk, 'title': 'Bomba linavuja',
                                 'priority': 'medium'}, follow=True)
        self.assertContains(resp, 'Asante, ombi lako limetumwa kwa mwenye nyumba.')
        resp = self.client.post(reverse('support:tenant_messages', args=[self.sw.tenant.portal_token]),
                                {'category': 'services', 'subject': 'Maji', 'message': 'Hakuna maji'}, follow=True)
        self.assertContains(resp, 'Umetumwa.')
        self.assertContains(resp, 'Tuma jibu')

    def test_receipt_and_statement_follow_the_tenant_not_the_viewer(self):
        payment = Payment.objects.create(lease=self.sw, amount=D('5000'), method='mpesa', reference='SJK1')
        statement = self.portal(self.sw, 'reports:tenant_statement', self.sw.pk)
        self.assertContains(statement, 'Taarifa ya akaunti')
        self.assertContains(statement, 'Kodi 5 Okt – 4 Nov 2026')  # rent day the 5th
        self.assertContains(statement, 'Malipo ya M-Pesa')
        self.assertContains(statement, 'Rudi')
        # The landlord printing it for the tenant gets the Kiswahili document with an English toolbar.
        self.client.force_login(self.owner)
        receipt = self.client.get(reverse('reports:payment_receipt', args=[payment.pk]))
        self.assertContains(receipt, 'Kiasi kilichopokelewa')
        self.assertContains(receipt, 'Print / Save as PDF')
        self.assertNotContains(receipt, 'Chapisha')
        # The rest of the landlord's app stays in English.
        self.assertContains(self.client.get(reverse('rentals:lease_detail', args=[self.sw.pk])), 'Kiswahili')
        self.assertContains(self.client.get(reverse('rentals:dashboard')), '<html lang="en')

    def test_charge_labels(self):
        from django.utils import translation
        deposit = RentCharge(lease=self.sw, kind='deposit', period=datetime.date(2026, 1, 1), description='Deposit')
        water = RentCharge(lease=self.sw, kind='water', period=datetime.date(2026, 10, 1),
                           description='Water 12 units (100 → 112) @ KES 150')
        custom = RentCharge(lease=self.sw, kind='other', period=datetime.date(2026, 10, 1), description='Parking')
        with translation.override('sw'):
            self.assertEqual(deposit.label, 'Amana')
            self.assertEqual(water.label, 'Maji uniti 12 (100 → 112) @ KES 150')
            self.assertEqual(custom.label, 'Parking')  # what the landlord typed is shown as written
        self.assertEqual(water.label, 'Water 12 units (100 → 112) @ KES 150')

    def test_landlord_sets_the_language(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('rentals:tenant_update', args=[self.en.tenant.pk]),
                         {'full_name': 'Tenant B1', 'phone': '0712345678', 'language': 'sw'})
        self.assertEqual(Tenant.objects.get(pk=self.en.tenant.pk).language, 'sw')

    # --- Announcements -------------------------------------------------------------------

    def test_announcement_uses_the_kiswahili_version_when_written(self):
        a = Announcement.objects.create(owner=self.owner, title='Water off', body='No water on Monday.',
                                        title_sw='Maji yatakatika', body_sw='Hakuna maji Jumatatu.')
        send_announcement(a)
        sms = {m.lease_id: m.body for m in SmsMessage.objects.filter(kind='announcement')}
        self.assertIn('Maji yatakatika. Hakuna maji Jumatatu.', sms[self.sw.pk])
        self.assertIn('Water off. No water on Monday.', sms[self.en.pk])
        self.assertContains(self.portal(self.sw), 'Hakuna maji Jumatatu.')
        self.assertContains(self.portal(self.en), 'No water on Monday.')

    def test_announcement_without_kiswahili_falls_back_to_english(self):
        a = Announcement.objects.create(owner=self.owner, title='Meeting', body='Saturday 10am.')
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('announcements:detail', args=[a.pk])),
                            '1 of them reads Kiswahili')
        send_announcement(a)
        self.assertIn('Meeting. Saturday 10am.', SmsMessage.objects.get(lease=self.sw).body)


class CatalogTests(TestCase):
    """Every string is translated, and the compiled .mo matches the .po (run compilemessages)."""

    def test_kiswahili_catalog_is_complete_and_compiled(self):
        po_path = settings.LOCALE_PATHS[0] / 'sw' / 'LC_MESSAGES' / 'django.po'
        text = po_path.read_text(encoding='utf-8')
        self.assertNotIn('#, fuzzy', text)
        unescape = lambda s: s.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')  # noqa: E731
        entries = dict(
            (unescape(m[0]), unescape(m[1]))
            for m in re.findall(r'^msgid "((?:[^"\\]|\\.)*)"\nmsgstr "((?:[^"\\]|\\.)*)"$', text, re.M) if m[0]
        )
        self.assertGreater(len(entries), 100)
        self.assertEqual([k for k, v in entries.items() if not v], [], 'untranslated strings')
        with open(po_path.with_suffix('.mo'), 'rb') as f:
            compiled = {k: v for k, v in gettext.GNUTranslations(f)._catalog.items() if k}
        self.assertEqual(compiled, entries, 'django.mo is out of date: run compilemessages')
        for msgid, msgstr in entries.items():
            self.assertEqual(sorted(re.findall(r'%\(\w+\)s', msgid)), sorted(re.findall(r'%\(\w+\)s', msgstr)), msgid)
