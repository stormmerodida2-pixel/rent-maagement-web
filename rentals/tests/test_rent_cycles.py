"""Rent days: a tenant who moves in on the 12th pays a full month every 12th (12 Oct – 11 Nov, …)."""

import datetime
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from ..models import Lease, Property, RentCharge, Tenant, Unit
from . import make_lease

D = datetime.date


class FakeDate(datetime.date):
    fixed = D(2026, 10, 1)

    @classmethod
    def today(cls):
        return cls.fixed


def run_billing_on(day):
    FakeDate.fixed = day
    fake = SimpleNamespace(date=FakeDate, datetime=datetime.datetime, timedelta=datetime.timedelta)
    with mock.patch('rentals.management.commands.generate_rent_charges.datetime', fake):
        call_command('generate_rent_charges', '--no-late-fees', stdout=StringIO())


class RentDayTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        prop = Property.objects.create(owner=self.owner, name='Riverside')
        self.unit = Unit.objects.create(property=prop, label='B1', default_rent=Decimal('18000'))
        self.tenant = Tenant.objects.create(owner=self.owner, full_name='Wanjiku Kamau', phone='0712345678')
        self.client.force_login(self.owner)

    def lease(self, start, due_day, **kwargs):
        return Lease.objects.create(unit=self.unit, tenant=self.tenant, start_date=start, monthly_rent=Decimal('18000'),
                                    due_day=due_day, **kwargs)

    def test_new_lease_rent_day_is_the_move_in_day(self):
        start = datetime.date.today().replace(day=12) if datetime.date.today().day >= 12 else \
            (datetime.date.today().replace(day=1) - datetime.timedelta(days=1)).replace(day=12)
        self.client.post(reverse('rentals:lease_create'), {
            'unit': self.unit.pk, 'tenant': self.tenant.pk, 'start_date': start.isoformat(), 'bill_now': 'on'})
        lease = Lease.objects.get()
        self.assertEqual(lease.due_day, 12)

    def test_a_month_runs_from_rent_day_to_the_day_before(self):
        lease = self.lease(D(2026, 10, 12), 12)
        charge, _ = lease.ensure_charge_for(D(2026, 10, 1))
        self.assertEqual(charge.due_date, D(2026, 10, 12))
        self.assertEqual(charge.label, 'Rent 12 Oct – 11 Nov 2026')
        nov, _ = lease.ensure_charge_for(D(2026, 11, 1))
        self.assertEqual(nov.label, 'Rent 12 Nov – 11 Dec 2026')
        self.assertEqual(nov.amount, Decimal('18000'))  # always a full month, no pro-rating

    def test_rent_day_31_falls_back_in_short_months(self):
        lease = self.lease(D(2026, 1, 31), 31)
        self.assertEqual(lease.cycle_start(D(2026, 2, 1)), D(2026, 2, 28))
        self.assertEqual(lease.cycle_start(D(2026, 4, 1)), D(2026, 4, 30))
        self.assertEqual(lease.cycle_end(D(2026, 1, 1)), D(2026, 2, 27))
        feb, _ = lease.ensure_charge_for(D(2026, 2, 1))
        self.assertEqual(feb.label, 'Rent 28 Feb – 30 Mar 2026')

    def test_billed_a_few_days_before_each_rent_day(self):
        early, late = self.lease(D(2026, 9, 3), 3), make_lease(self.owner, label='C1', start=D(2026, 9, 25), due_day=25)
        run_billing_on(D(2026, 10, 1))
        self.assertTrue(early.charges.filter(period=D(2026, 10, 1), kind='rent').exists())
        self.assertFalse(late.charges.filter(period=D(2026, 10, 1), kind='rent').exists())  # not owed yet
        run_billing_on(D(2026, 10, 20))  # 5 days before the 25th
        self.assertTrue(late.charges.filter(period=D(2026, 10, 1), kind='rent').exists())

    def test_existing_leases_on_the_5th_still_bill_on_the_1st(self):
        lease = make_lease(self.owner, label='C2', start=D(2026, 1, 15), due_day=5)
        self.assertEqual(lease.bill_from(D(2026, 10, 1)), D(2026, 10, 1))

    def test_no_new_month_after_moving_out(self):
        lease = self.lease(D(2026, 10, 12), 12, end_date=D(2026, 12, 11))
        self.assertTrue(lease.ensure_charge_for(D(2026, 11, 1))[1])  # 12 Nov – 11 Dec: they live there
        self.assertEqual(lease.ensure_charge_for(D(2026, 12, 1)), (None, False))  # 12 Dec would be after they leave

    def test_move_out_defaults_to_the_end_of_a_rent_month(self):
        lease = self.lease(D(2026, 10, 12), 12)
        self.assertEqual(lease.default_move_out(D(2026, 10, 14)), D(2026, 12, 11))
        self.assertEqual(lease.default_move_out(D(2026, 10, 1)), D(2026, 11, 11))

    def test_lease_page_shows_the_rent_day(self):
        lease = self.lease(D(2026, 10, 12), 12)
        self.assertContains(self.client.get(reverse('rentals:lease_detail', args=[lease.pk])), 'rent day: the 12th')

    def test_statement_dates_rent_on_its_rent_day(self):
        lease = self.lease(D(2026, 10, 12), 12)
        lease.ensure_charge_for(D(2026, 10, 1))
        from ..views import statement
        rent = next(e for e in statement(lease) if e.get('charge_id'))
        self.assertEqual(rent['date'], D(2026, 10, 12))
        self.assertEqual(RentCharge.objects.count(), 1)
