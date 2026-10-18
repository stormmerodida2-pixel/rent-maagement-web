"""KRA rental income tax, move-in/move-out inspections, and the tenancy agreement and documents."""

import datetime
import io
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from payments.models import Payment
from reports.models import TaxFiling
from reports.tax import due_now, month_rent, send_tax_reminders, tax_year

from ..models import Inspection, InspectionPhoto, Lease, Property, RentCharge, Tenant, TenantDocument, Unit

D = Decimal
PRIVATE = tempfile.mkdtemp()


def at(day):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(10)))


def photo(name='room.jpg'):
    out = io.BytesIO()
    Image.new('RGB', (40, 30), 'blue').save(out, 'JPEG')
    return SimpleUploadedFile(name, out.getvalue(), content_type='image/jpeg')


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', first_name='Grace', last_name='Wanjiru',
                                                          email='grace@example.com')
        from accounts.utils import mark_verified

        mark_verified(self.owner)
        self.prop = Property.objects.create(owner=self.owner, name='Kodi Apartments', address='Ngong Road')
        self.lease = self.let('A1', 'Jane Akinyi', '20000')

    def let(self, label, name, rent, unit_type=Unit.Type.ONE_BEDROOM, start=datetime.date(2026, 1, 3)):
        unit = Unit.objects.create(property=self.prop, label=label, default_rent=D(rent), unit_type=unit_type)
        tenant = Tenant.objects.create(owner=self.owner, full_name=name, phone='0712345678')
        return Lease.objects.create(unit=unit, tenant=tenant, start_date=start, monthly_rent=D(rent),
                                    deposit=D(rent), due_day=start.day)


class TaxTests(Base):
    def test_residential_rent_and_tax(self):
        shop = self.let('S1', 'Duka Ltd', '30000', unit_type=Unit.Type.SHOP)
        june = datetime.date(2026, 6, 1)
        Payment.objects.create(lease=self.lease, amount=D('210000'), method='cash', paid_at=at(june))
        Payment.objects.create(lease=shop, amount=D('30000'), method='cash', paid_at=at(june))
        rent = month_rent(self.owner, june)
        self.assertEqual((rent['residential'], rent['commercial']), (D('210000'), D('30000')))
        row = next(r for r in tax_year(self.owner, 2026, today=datetime.date(2026, 7, 16))['rows']
                   if r['month'] == june)
        self.assertEqual(row['tax'], D('15750'))  # 7.5%
        self.assertEqual(row['due'], datetime.date(2026, 7, 20))
        self.assertEqual(row['status'], 'due')

    def test_new_tenants_deposit_is_not_rent(self):
        july = datetime.date(2026, 7, 1)
        newcomer = self.let('A2', 'Musa', '15000', start=datetime.date(2026, 7, 5))
        RentCharge.objects.create(lease=newcomer, period=july, due_date=july, amount=D('15000'),
                                  kind=RentCharge.Kind.DEPOSIT)
        Payment.objects.create(lease=newcomer, amount=D('30000'), method='cash', paid_at=at(datetime.date(2026, 7, 5)))
        self.assertEqual(month_rent(self.owner, july)['residential'], D('15000'))

    def test_reminder_on_the_15th_until_filed(self):
        Payment.objects.create(lease=self.lease, amount=D('40000'), method='cash', paid_at=at(datetime.date(2026, 6, 3)))
        with self.settings():
            self.assertIsNone(due_now(self.owner, datetime.date(2026, 7, 10)))
            self.assertEqual(due_now(self.owner, datetime.date(2026, 7, 15))['tax'], D('3000'))
        # Below KES 288,000 a year at this rate: MRI may not apply, so no nagging.
        self.assertEqual(send_tax_reminders(datetime.date(2026, 7, 15)), 0)
        Payment.objects.create(lease=self.lease, amount=D('400000'), method='cash', paid_at=at(datetime.date(2026, 6, 4)))
        self.assertEqual(send_tax_reminders(datetime.date(2026, 7, 15)), 1)
        self.assertIn('rental income tax', mail.outbox[-1].subject)
        TaxFiling.objects.create(owner=self.owner, month=datetime.date(2026, 6, 1), amount=D('33000'),
                                 filed_on=datetime.date(2026, 7, 16))
        self.assertIsNone(due_now(self.owner, datetime.date(2026, 7, 17)))

    def test_page_mark_filed_and_csv(self):
        Payment.objects.create(lease=self.lease, amount=D('20000'), method='cash',
                               paid_at=at(datetime.date(2026, 1, 3)))
        self.client.force_login(self.owner)
        page = self.client.get(reverse('reports:tax') + '?year=2026')
        self.assertContains(page, 'KRA rental income tax 2026')
        self.assertContains(page, 'KES 1,500')
        self.client.post(reverse('reports:tax_filed'), {'month': '2026-01', 'reference': '2020ABC', 'amount': '1500'})
        filing = TaxFiling.objects.get()
        self.assertEqual((filing.amount, filing.reference), (D('1500'), '2020ABC'))
        csv = self.client.get(reverse('reports:tax') + '?year=2026&format=csv').content.decode()
        self.assertIn('January 2026,20000', csv)
        self.assertIn('2020ABC', csv)


@override_settings(PRIVATE_MEDIA_ROOT=PRIVATE)
class InspectionTests(Base):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(PRIVATE, ignore_errors=True)

    def test_photos_in_and_out_side_by_side(self):
        self.client.force_login(self.owner)
        url_in = reverse('rentals:inspection', args=[self.lease.pk, 'move_in'])
        self.client.post(url_in, {'room': 'Kitchen', 'note': 'Sink fine', 'photos': [photo(), photo('b.jpg')]})
        self.client.post(reverse('rentals:inspection', args=[self.lease.pk, 'move_out']),
                         {'room': 'Kitchen', 'note': 'Sink cracked', 'photos': [photo()]})
        self.assertEqual(InspectionPhoto.objects.count(), 3)
        compare = self.client.get(reverse('rentals:inspection_compare', args=[self.lease.pk]))
        self.assertContains(compare, 'Sink fine')
        self.assertContains(compare, 'Sink cracked')
        first = InspectionPhoto.objects.first()
        self.assertEqual(self.client.get(reverse('private_file', args=['inspection', first.pk])).status_code, 200)

        # The tenant sees the same photos from their rent page; nobody else can.
        token = self.lease.tenant.portal_token
        portal = self.client.get(reverse('rentals:tenant_portal', args=[token]))
        self.assertContains(portal, reverse('rentals:tenant_inspection', args=[token, self.lease.pk]))
        self.assertContains(self.client.get(reverse('rentals:tenant_inspection', args=[token, self.lease.pk])),
                            'Sink cracked')
        self.assertEqual(self.client.get(reverse('rentals:tenant_inspection_photo', args=[token, first.pk])).status_code,
                         200)
        other = self.let('A2', 'Musa', '15000')
        self.assertEqual(self.client.get(reverse('rentals:tenant_inspection_photo',
                                                 args=[other.tenant.portal_token, first.pk])).status_code, 404)

    def test_notes_and_delete(self):
        self.client.force_login(self.owner)
        url = reverse('rentals:inspection', args=[self.lease.pk, 'move_in'])
        self.client.post(url, {'notes': '2 keys handed over', 'done_on': '2026-01-03'})
        self.assertEqual(Inspection.objects.get().notes, '2 keys handed over')
        self.client.post(url, {'room': 'Bathroom', 'photos': [photo()]})
        self.client.post(reverse('rentals:inspection_photo_delete', args=[InspectionPhoto.objects.get().pk]))
        self.assertFalse(InspectionPhoto.objects.exists())

    def test_other_landlords_cannot_see(self):
        stranger = get_user_model().objects.create_user('stranger')
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse('rentals:inspection', args=[self.lease.pk, 'move_in'])).status_code,
                         404)


@override_settings(PRIVATE_MEDIA_ROOT=PRIVATE)
class AgreementAndDocumentTests(Base):
    def test_agreement_filled_in(self):
        from accounts.models import profile_for

        profile = profile_for(self.owner)
        profile.agreement_clauses = 'No pets without written permission.\nGate closes at 11pm.'
        profile.save()
        self.lease.tenant.id_number = '12345678'
        self.lease.tenant.save()
        self.client.force_login(self.owner)
        page = self.client.get(reverse('rentals:agreement', args=[self.lease.pk]))
        for text in ('Tenancy agreement', 'Jane Akinyi', '12345678', 'Grace Wanjiru', 'KES 20,000 a month',
                     'day <strong>3</strong>', "one month's written notice", 'No pets without written permission.',
                     'Gate closes at 11pm.', 'Kodi Apartments'):
            self.assertContains(page, text)

    def test_upload_view_delete_documents(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('rentals:document_upload', args=[self.lease.pk]), {'kind': 'id', 'file': photo()})
        pdf = SimpleUploadedFile('agreement.pdf', b'%PDF-1.4 fake pdf', content_type='application/pdf')
        self.client.post(reverse('rentals:document_upload', args=[self.lease.pk]), {'kind': 'agreement', 'file': pdf})
        docs = {d.kind: d for d in TenantDocument.objects.all()}
        self.assertEqual(docs['agreement'].content_type, 'application/pdf')
        self.assertEqual(docs['id'].content_type, 'image/jpeg')
        page = self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.assertContains(page, 'Signed tenancy agreement')
        resp = self.client.get(reverse('rentals:document_file', args=[docs['agreement'].pk]))
        self.assertEqual(resp['Content-Type'], 'application/pdf')

        bad = SimpleUploadedFile('x.exe', b'MZ not a photo', content_type='application/octet-stream')
        self.client.post(reverse('rentals:document_upload', args=[self.lease.pk]), {'kind': 'other', 'file': bad})
        self.assertEqual(TenantDocument.objects.count(), 2)

        stranger = get_user_model().objects.create_user('stranger')
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse('rentals:document_file', args=[docs['id'].pk])).status_code, 404)
        self.client.force_login(self.owner)
        self.client.post(reverse('rentals:document_delete', args=[docs['id'].pk]))
        self.assertEqual(TenantDocument.objects.count(), 1)
