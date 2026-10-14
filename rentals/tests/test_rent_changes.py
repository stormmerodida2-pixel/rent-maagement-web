"""Planned rent changes for month-to-month tenants: one lease or a whole property."""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEntry
from notifications.models import SmsMessage

from ..models import Lease, Property, RentChange, Tenant, Unit
from ..utils import new_rent_for

D = datetime.date
TODAY = datetime.date.today()
FUTURE = (TODAY + datetime.timedelta(days=62)).replace(day=1)


class RentChangeTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.prop = Property.objects.create(owner=self.owner, name='Riverside')
        self.leases = []
        for label, rent, day in (('B1', '18000', 12), ('B2', '20000', 5), ('B3', '15000', 25)):
            unit = Unit.objects.create(property=self.prop, label=label, default_rent=Decimal(rent))
            tenant = Tenant.objects.create(owner=self.owner, full_name=f'Tenant {label}', phone='0712345678')
            self.leases.append(Lease.objects.create(unit=unit, tenant=tenant, start_date=D(2026, 1, day),
                                                    monthly_rent=Decimal(rent), due_day=day))
        self.lease = self.leases[0]
        self.client.force_login(self.owner)

    def test_rounding(self):
        self.assertEqual(new_rent_for(Decimal('18000'), 'percent', Decimal('5')), Decimal('18900'))
        self.assertEqual(new_rent_for(Decimal('18500'), 'percent', Decimal('7')), Decimal('19800'))  # nearest 50
        self.assertEqual(new_rent_for(Decimal('18000'), 'amount', Decimal('-1000')), Decimal('17000'))
        self.assertEqual(new_rent_for(Decimal('18000'), 'set', Decimal('21000')), Decimal('21000'))

    def test_plan_for_one_tenant_tells_them_and_switches_on_their_rent_day(self):
        resp = self.client.post(reverse('rentals:rent_change', args=[self.lease.pk]),
                                {'new_rent': '20000', 'effective_from': FUTURE.isoformat(), 'notify': 'on'})
        self.assertRedirects(resp, reverse('rentals:lease_detail', args=[self.lease.pk]))
        change = RentChange.objects.get()
        self.assertEqual(change.starts_on, FUTURE.replace(day=12))  # their first rent day on/after the date
        self.assertIsNotNone(change.notified_at)
        sms = SmsMessage.objects.get()
        self.assertIn('from KES 18,000 to KES 20,000 a month', sms.body)
        self.assertIn(f'{change.starts_on.day} {change.starts_on:%b %Y}', sms.body)
        page = self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.assertContains(page, 'Rent changes to KES 20,000')
        portal = self.client.get(reverse('rentals:tenant_portal', args=[self.lease.tenant.portal_token]))
        self.assertContains(portal, 'Your rent changes to KES 20,000 a month')

        # The month before still bills the old rent; the change month bills the new one and makes it stick.
        before = (FUTURE - datetime.timedelta(days=1)).replace(day=1)
        self.assertEqual(self.lease.ensure_charge_for(before)[0].amount, Decimal('18000'))
        self.assertEqual(self.lease.ensure_charge_for(FUTURE)[0].amount, Decimal('20000'))
        self.lease.refresh_from_db()
        change.refresh_from_db()
        self.assertEqual(self.lease.monthly_rent, Decimal('20000'))
        self.assertIsNotNone(change.applied_at)
        self.assertTrue(AuditEntry.objects.filter(action='lease.rent_change_planned').exists())

    def test_without_sms_and_cancel(self):
        self.client.post(reverse('rentals:rent_change', args=[self.lease.pk]),
                         {'new_rent': '19000', 'effective_from': FUTURE.isoformat()})
        self.assertFalse(SmsMessage.objects.exists())
        self.client.post(reverse('rentals:rent_change', args=[self.lease.pk]), {'action': 'cancel'})
        self.assertFalse(RentChange.objects.exists())
        self.assertEqual(self.lease.ensure_charge_for(FUTURE)[0].amount, Decimal('18000'))

    def test_a_new_plan_replaces_the_old_one(self):
        for rent in ('19000', '21000'):
            self.client.post(reverse('rentals:rent_change', args=[self.lease.pk]),
                             {'new_rent': rent, 'effective_from': FUTURE.isoformat()})
        self.assertEqual(list(RentChange.objects.values_list('new_rent', flat=True)), [Decimal('21000')])

    def test_dates_must_be_in_the_future(self):
        resp = self.client.post(reverse('rentals:rent_change', args=[self.lease.pk]),
                                {'new_rent': '20000', 'effective_from': TODAY.isoformat()})
        self.assertContains(resp, 'Choose a date in the future.')

    def test_whole_property_preview_then_confirm(self):
        url = reverse('rentals:property_rent_change', args=[self.prop.pk])
        data = {'mode': 'percent', 'value': '10', 'effective_from': FUTURE.isoformat(),
                'leases': [lease.pk for lease in self.leases], 'notify': 'on'}
        preview = self.client.post(url, {**data, 'preview': '1'})
        self.assertContains(preview, 'KES 19,800')  # 18,000 + 10%
        self.assertContains(preview, 'KES 22,000')  # 20,000 + 10%
        self.assertContains(preview, 'KES 16,500')  # 15,000 + 10%
        self.assertFalse(RentChange.objects.exists())  # nothing saved until confirmed
        self.client.post(url, {**data, 'confirm': '1'})
        self.assertEqual(RentChange.objects.count(), 3)
        self.assertEqual(SmsMessage.objects.count(), 3)
        starts = sorted(c.starts_on.day for c in RentChange.objects.all())
        self.assertEqual(starts, [5, 12, 25])  # each on their own rent day

    def test_property_change_skips_tenants_moving_out(self):
        Lease.objects.filter(pk=self.leases[2].pk).update(end_date=TODAY + datetime.timedelta(days=20))
        page = self.client.get(reverse('rentals:property_rent_change', args=[self.prop.pk]))
        self.assertNotContains(page, 'Tenant B3')

    def test_other_landlords_cannot(self):
        stranger = get_user_model().objects.create_user('other')
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse('rentals:rent_change', args=[self.lease.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('rentals:property_rent_change', args=[self.prop.pk])).status_code,
                         404)
