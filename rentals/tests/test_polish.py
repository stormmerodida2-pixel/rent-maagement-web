"""Log repair from a house, the tenant's full history, bulk actions, and the landlord's look on receipts."""

import io
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from maintenance.models import MaintenanceRequest
from notifications.models import SmsMessage
from payments.models import Payment

from ..models import Lease, RentCharge, TenantNote
from . import make_lease

D = Decimal
MEDIA = tempfile.mkdtemp()


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', first_name='Grace')
        self.lease = make_lease(self.owner, label='A1')
        self.client.force_login(self.owner)


class LogRepairFromHouseTests(Base):
    def test_buttons_fill_in_the_house(self):
        page = self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.assertContains(page, f'?unit={self.lease.unit_id}&amp;tenant={self.lease.tenant_id}')
        form = self.client.get(reverse('maintenance:create') + f'?unit={self.lease.unit_id}&tenant={self.lease.tenant_id}')
        self.assertEqual(form.context['form'].initial['where'], f'u{self.lease.unit_id}')
        self.assertEqual(form.context['form'].initial['reported_by'], self.lease.tenant_id)
        prop = self.client.get(reverse('maintenance:create') + f'?property={self.lease.unit.property_id}')
        self.assertEqual(prop.context['form'].initial['where'], f'p{self.lease.unit.property_id}')

    def test_back_where_you_came_from(self):
        back = reverse('rentals:lease_detail', args=[self.lease.pk])
        resp = self.client.post(reverse('maintenance:create'), {
            'where': f'u{self.lease.unit_id}', 'title': 'Door', 'priority': 'low', 'next': back})
        self.assertRedirects(resp, back, fetch_redirect_response=False)


class HistoryTests(Base):
    def test_everything_on_one_timeline(self):
        Payment.objects.create(lease=self.lease, amount=D('15000'), method='cash')
        MaintenanceRequest.objects.create(unit=self.lease.unit, title='Leaking tap', reported_by=self.lease.tenant)
        TenantNote.objects.create(lease=self.lease, text='Promised to pay Friday', author=self.owner)
        SmsMessage.objects.create(lease=self.lease, phone='254712345678', body='Hi, your rent…',
                                  kind=SmsMessage.Kind.REMINDER, status=SmsMessage.Status.SENT)
        url = reverse('rentals:tenant_history', args=[self.lease.pk])
        self.assertContains(self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk])), url)
        page = self.client.get(url)
        for text in ('Paid KES 15,000', 'Repair logged: Leaking tap', 'Promised to pay Friday',
                     'Text sent: rent reminder', 'Moved into'):
            self.assertContains(page, text)
        only = self.client.get(url + '?show=payment')
        self.assertContains(only, 'Paid KES 15,000')
        self.assertNotContains(only, 'Leaking tap')

    def test_other_landlords_tenants_are_private(self):
        other = make_lease(get_user_model().objects.create_user('other'), label='Z9')
        self.assertEqual(self.client.get(reverse('rentals:tenant_history', args=[other.pk])).status_code, 404)


class BulkTests(Base):
    def test_mark_several_repairs_done_and_text_tenants(self):
        a = MaintenanceRequest.objects.create(unit=self.lease.unit, title='Tap')
        b = MaintenanceRequest.objects.create(unit=self.lease.unit, title='Door')
        c = MaintenanceRequest.objects.create(unit=self.lease.unit, title='Window')
        self.assertContains(self.client.get(reverse('maintenance:list')), 'Mark selected done')
        self.client.post(reverse('maintenance:bulk_resolve'), {'repair': [a.pk, b.pk], 'notify_tenants': 'on'})
        statuses = dict(MaintenanceRequest.objects.values_list('title', 'status'))
        self.assertEqual(statuses, {'Tap': 'resolved', 'Door': 'resolved', 'Window': 'open'})
        self.assertEqual(SmsMessage.objects.count(), 2)
        self.assertIsNotNone(MaintenanceRequest.objects.get(pk=a.pk).resolved_at)
        self.assertTrue(c.pk)

    def test_cannot_touch_other_landlords_repairs(self):
        other = make_lease(get_user_model().objects.create_user('other'), label='Z9')
        theirs = MaintenanceRequest.objects.create(unit=other.unit, title='Theirs')
        self.client.post(reverse('maintenance:bulk_resolve'), {'repair': [theirs.pk]})
        self.assertEqual(MaintenanceRequest.objects.get(pk=theirs.pk).status, 'open')

    def test_remind_selected_from_the_tenants_list(self):
        second = make_lease(self.owner, label='A2')
        for lease in (self.lease, second):
            RentCharge.objects.create(lease=lease, period=lease.start_date.replace(day=1), due_date=lease.start_date,
                                      amount=D('15000'), kind=RentCharge.Kind.RENT)
        listing = self.client.get(reverse('rentals:lease_list') + '?status=active&owing=1')
        self.assertContains(listing, 'Remind selected')
        preview = self.client.get(reverse('rentals:remind_all') + f'?lease={second.pk}')
        checked = {r['lease'].pk: r['checked'] for r in preview.context['rows']}
        self.assertEqual(checked, {self.lease.pk: False, second.pk: True})
        self.assertEqual(preview.context['selected_count'], 1)


@override_settings(MEDIA_ROOT=MEDIA)
class BrandingTests(Base):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def logo(self):
        out = io.BytesIO()
        Image.new('RGBA', (1200, 400), (14, 122, 75, 0)).save(out, 'PNG')
        return SimpleUploadedFile('logo.png', out.getvalue(), content_type='image/png')

    def test_name_logo_and_colour_on_receipts(self):
        self.assertContains(self.client.get(reverse('accounts:settings')), 'Receipts and statements')
        self.client.post(reverse('accounts:branding'), {'brand_name': 'Kodi Properties Ltd', 'brand_color': '#aa3366',
                                                        'logo': self.logo()})
        profile = self.owner.profile
        profile.refresh_from_db()
        self.assertTrue(profile.brand_logo.name.endswith('.png'))
        with Image.open(profile.brand_logo.path) as img:
            self.assertLessEqual(img.width, 600)
            self.assertEqual(img.mode, 'RGBA')  # transparency kept

        payment = Payment.objects.create(lease=self.lease, amount=D('15000'), method='cash')
        receipt = self.client.get(reverse('reports:payment_receipt', args=[payment.pk]))
        self.assertContains(receipt, 'Kodi Properties Ltd')
        self.assertContains(receipt, '--doc-accent: #aa3366')
        self.assertContains(receipt, profile.brand_logo.url)
        statement = self.client.get(reverse('rentals:tenant_portal', args=[self.lease.tenant.portal_token]))
        self.assertEqual(statement.status_code, 200)

    def test_bad_colour_is_refused(self):
        resp = self.client.post(reverse('accounts:branding'), {'brand_name': 'X', 'brand_color': 'red;}body{'})
        self.assertEqual(resp.status_code, 200)
        self.owner.profile.refresh_from_db()
        self.assertEqual(self.owner.profile.brand_color, '')

    def test_unbranded_receipts_unchanged(self):
        payment = Payment.objects.create(lease=self.lease, amount=D('15000'), method='cash')
        receipt = self.client.get(reverse('reports:payment_receipt', args=[payment.pk]))
        self.assertNotContains(receipt, '--doc-accent')
        self.assertTrue(Lease.objects.exists())


class LiveRefreshTests(Base):
    def test_reminders_from_refreshed_dashboard_parts_come_back_to_the_dashboard(self):
        RentCharge.objects.create(lease=self.lease, period=self.lease.start_date.replace(day=1),
                                  due_date=self.lease.start_date, amount=D('15000'), kind=RentCharge.Kind.RENT)
        fragments = self.client.get(reverse('rentals:live') + '?since=0&fragments=1').json()['fragments']
        self.assertIn(f'name="next" value="{reverse("rentals:dashboard")}"', fragments['arrears'])
        self.assertNotIn('/live/', fragments['arrears'])
        resp = self.client.post(reverse('rentals:send_reminder', args=[self.lease.pk]),
                                {'next': reverse('rentals:dashboard')})
        self.assertRedirects(resp, reverse('rentals:dashboard'), fetch_redirect_response=False)
