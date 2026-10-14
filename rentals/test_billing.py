"""Service charges, late fees, deposits and move-out settlement, water billing, one-off charges."""

import datetime
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEntry
from payments.models import Payment

from .billing import BillingError, record_water_reading, settle_move_out
from .models import Lease, MeterReading, Property, RentCharge, Tenant, Unit
from .tests import make_lease
from .views import statement

D = Decimal


class ChargesTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner, rent='10000', due_day=5)

    def test_service_charge_billed_with_rent_once(self):
        self.lease.service_charge = D('500')
        self.lease.save()
        self.lease.ensure_charge_for(datetime.date(2026, 3, 1))
        self.lease.ensure_charge_for(datetime.date(2026, 3, 15))
        kinds = sorted(self.lease.charges.values_list('kind', flat=True))
        self.assertEqual(kinds, ['rent', 'service'])
        self.assertEqual(self.lease.balance, D('10500'))

    def test_late_fee_after_grace_once_and_only_if_unpaid(self):
        self.lease.late_fee, self.lease.late_fee_grace_days = D('1000'), 3
        self.lease.save()
        self.lease.ensure_charge_for(datetime.date(2026, 3, 1))  # due 5 Mar
        self.assertIsNone(self.lease.apply_late_fee(datetime.date(2026, 3, 8)))  # still in grace
        fee = self.lease.apply_late_fee(datetime.date(2026, 3, 9))
        self.assertEqual((fee.amount, fee.label), (D('1000'), 'Late fee for March 2026'))
        self.assertIsNone(self.lease.apply_late_fee(datetime.date(2026, 3, 20)))  # once per month

    def test_no_late_fee_when_paid(self):
        self.lease.late_fee = D('1000')
        self.lease.save()
        self.lease.ensure_charge_for(datetime.date(2026, 3, 1))
        Payment.objects.create(lease=self.lease, amount=D('10000'), method='cash')
        self.assertIsNone(self.lease.apply_late_fee(datetime.date(2026, 3, 20)))

    def test_command_applies_late_fees(self):
        today = datetime.date.today()
        self.lease.late_fee, self.lease.late_fee_grace_days, self.lease.due_day = D('700'), 0, 1
        self.lease.save()
        if today.day == 1:
            self.skipTest('Due date is today; nothing is late yet.')
        call_command('generate_rent_charges', stdout=StringIO())
        self.assertTrue(self.lease.charges.filter(kind='late_fee', amount=D('700')).exists())

    def test_deposit_billed_once_and_held(self):
        self.lease.deposit = D('20000')
        self.lease.save()
        self.lease.bill_deposit()
        self.assertIsNone(self.lease.bill_deposit())
        self.assertEqual(self.lease.deposit_held, D('20000'))


class MoveOutTests(TestCase):
    def setUp(self):
        owner = get_user_model().objects.create_user('landlord')
        self.lease = make_lease(owner, rent='10000', deposit='20000')
        self.lease.bill_deposit()
        self.lease.ensure_charge_for(datetime.date(2026, 1, 1))
        Payment.objects.create(lease=self.lease, amount=D('20000'), method='cash')  # deposit paid
        # Rent of 10,000 unpaid.

    def test_deposit_covers_arrears_and_deductions_refund_due(self):
        final = settle_move_out(self.lease, datetime.date(2026, 2, 28), deductions=D('3000'), reason='Broken door')
        self.assertEqual(final, D('-7000'))  # 20,000 - 10,000 rent - 3,000 deduction
        self.assertFalse(self.lease.is_active)
        labels = list(self.lease.charges.values_list('description', flat=True))
        self.assertIn('Move-out deduction: Broken door', labels)
        self.assertEqual(self.lease.deposit_held, 0)

    def test_refund_paid_closes_account(self):
        final = settle_move_out(self.lease, datetime.date(2026, 2, 28), refund_paid=True)
        self.assertEqual(final, 0)
        self.assertEqual(self.lease.balance, 0)
        self.assertTrue(self.lease.charges.filter(kind='refund', amount=D('10000')).exists())

    def test_tenant_still_owes_when_deposit_is_not_enough(self):
        final = settle_move_out(self.lease, datetime.date(2026, 2, 28), deductions=D('25000'), reason='Rewiring')
        self.assertEqual(final, D('15000'))

    def test_end_lease_page_requires_reason_for_deductions(self):
        self.client.force_login(self.lease.unit.property.owner)
        url = reverse('rentals:lease_end', args=[self.lease.pk])
        resp = self.client.post(url, {'end_date': '2026-02-28', 'deductions': '500', 'apply_deposit': 'on'})
        self.assertContains(resp, 'Say what the deductions are for')
        resp = self.client.post(url, {'end_date': '2026-02-28', 'deductions': '500', 'reason': 'Cleaning',
                                      'apply_deposit': 'on'}, follow=True)
        self.assertContains(resp, 'Refund due to the tenant: KES 9,500')

    def test_statement_shows_credits(self):
        settle_move_out(self.lease, datetime.date(2026, 2, 28))
        credit = next(e for e in statement(self.lease) if e['description'] == 'Deposit applied at move-out')
        self.assertEqual((credit['payment'], credit.get('credit')), (D('20000'), True))
        self.assertEqual(statement(self.lease)[0]['balance'], self.lease.balance)


class WaterTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner)
        self.unit = self.lease.unit
        self.unit.property.water_rate = D('150')
        self.unit.property.save()

    def test_first_reading_is_baseline_then_bills_consumption(self):
        first = record_water_reading(self.unit, datetime.date(2026, 2, 1), '1200')
        self.assertIsNone(first.charge)
        second = record_water_reading(self.unit, datetime.date(2026, 3, 1), '1212.5')
        self.assertEqual(second.charge.amount, D('1875.00'))  # 12.5 units × 150
        self.assertEqual(second.charge.kind, 'water')
        self.assertIn('12.5 units', second.charge.label)

    def test_rejects_lower_reading_or_earlier_date(self):
        record_water_reading(self.unit, datetime.date(2026, 2, 1), '1200')
        with self.assertRaises(BillingError):
            record_water_reading(self.unit, datetime.date(2026, 3, 1), '1100')
        with self.assertRaises(BillingError):
            record_water_reading(self.unit, datetime.date(2026, 1, 1), '1300')

    def test_vacant_unit_or_no_rate_records_without_billing(self):
        vacant = Unit.objects.create(property=self.unit.property, label='V1', default_rent=1)
        record_water_reading(vacant, datetime.date(2026, 2, 1), '10')
        self.assertIsNone(record_water_reading(vacant, datetime.date(2026, 3, 1), '20').charge)

    def test_readings_page_bills_all_units(self):
        other = make_lease(self.owner, label='A2')
        for unit in (self.unit, other.unit):
            record_water_reading(unit, datetime.date(2026, 2, 1), '100')
        self.client.force_login(self.owner)
        url = reverse('rentals:water_readings', args=[self.unit.property.pk])
        resp = self.client.post(url, {
            'reading_date': '2026-03-01', f'unit_{self.unit.pk}': '110', f'unit_{other.unit.pk}': '90',
        }, follow=True)
        self.assertContains(resp, 'Saved 1 reading(s); billed KES 1,500 for water.')
        self.assertContains(resp, 'lower than the last reading')
        self.assertEqual(MeterReading.objects.count(), 3)

    def test_other_landlord_cannot_use_readings_page(self):
        intruder = get_user_model().objects.create_user('intruder')
        self.client.force_login(intruder)
        resp = self.client.get(reverse('rentals:water_readings', args=[self.unit.property.pk]))
        self.assertEqual(resp.status_code, 404)


class ChargeCreateTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord')
        self.lease = make_lease(self.owner)
        self.client.force_login(self.owner)

    def test_one_off_charge_and_credit(self):
        url = reverse('rentals:charge_create', args=[self.lease.pk])
        self.client.post(url, {'kind': 'other', 'description': 'Replaced window', 'amount': '2500', 'due_date': '2026-03-10'})
        self.client.post(url, {'kind': 'other', 'description': 'Goodwill credit', 'amount': '-500', 'due_date': '2026-03-11'})
        self.assertEqual(self.lease.balance, D('2000'))
        self.assertEqual(self.lease.charges.get(amount=D('2500')).period, datetime.date(2026, 3, 1))

    def test_rent_kind_not_allowed_and_scoped(self):
        url = reverse('rentals:charge_create', args=[self.lease.pk])
        self.client.post(url, {'kind': 'rent', 'description': 'x', 'amount': '1', 'due_date': '2026-03-10'})
        self.assertFalse(RentCharge.objects.exists())
        other = make_lease(get_user_model().objects.create_user('bob'), label='B1')
        self.assertEqual(self.client.get(reverse('rentals:charge_create', args=[other.pk])).status_code, 404)


class LeaseFormMoneyTests(TestCase):
    def test_new_lease_bills_deposit_and_service(self):
        owner = get_user_model().objects.create_user('landlord')
        prop = Property.objects.create(owner=owner, name='P')
        unit = Unit.objects.create(property=prop, label='A1', default_rent=10000)
        tenant = Tenant.objects.create(owner=owner, full_name='T', phone='0712345678')
        self.client.force_login(owner)
        self.client.post(reverse('rentals:lease_create'), {
            'unit': unit.pk, 'tenant': tenant.pk, 'start_date': datetime.date.today().isoformat(),
            'deposit': '10000', 'service_charge': '300', 'due_day': '5', 'bill_now': 'on', 'bill_deposit': 'on',
        })
        lease = Lease.objects.get()
        self.assertEqual(sorted(lease.charges.values_list('kind', flat=True)), ['deposit', 'rent', 'service'])
        self.assertEqual((lease.late_fee, lease.late_fee_grace_days), (0, 3))
        self.assertEqual(lease.balance, D('20300'))


class VoidReadingTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord')
        self.lease = make_lease(self.owner)
        self.unit = self.lease.unit
        self.unit.property.water_rate = D('100')
        self.unit.property.save()
        record_water_reading(self.unit, datetime.date(2026, 2, 1), '1000')
        self.client.force_login(self.owner)

    def test_typo_can_be_voided_and_re_entered(self):
        typo = record_water_reading(self.unit, datetime.date(2026, 3, 1), '10010')  # meant 1010
        self.assertEqual(self.lease.balance, D('901000'))
        # Without the fix, every later reading is "lower than the last reading".
        with self.assertRaises(BillingError):
            record_water_reading(self.unit, datetime.date(2026, 3, 1), '1010')

        resp = self.client.post(reverse('rentals:reading_void', args=[typo.pk]), {'reason': 'Typo'}, follow=True)
        self.assertContains(resp, 'You can now enter the correct one')
        self.assertEqual(self.lease.balance, 0)
        typo = MeterReading.all_objects.get(pk=typo.pk)
        self.assertTrue(typo.is_voided)
        self.assertTrue(RentCharge.all_objects.get(pk=typo.charge_id).is_voided)

        fixed = record_water_reading(self.unit, datetime.date(2026, 3, 1), '1010')
        self.assertEqual(fixed.charge.amount, D('1000.00'))  # 10 units × 100, from the 1000 baseline
        self.assertTrue(AuditEntry.objects.filter(action='reading.voided', lease=self.lease).exists())

    def test_only_latest_reading_can_be_voided(self):
        first = MeterReading.objects.get()
        record_water_reading(self.unit, datetime.date(2026, 3, 1), '1010')
        resp = self.client.post(reverse('rentals:reading_void', args=[first.pk]), {'reason': 'x'}, follow=True)
        self.assertContains(resp, 'only the latest reading can be voided')
        self.assertFalse(MeterReading.all_objects.get(pk=first.pk).is_voided)

    def test_other_landlord_cannot_void(self):
        reading = MeterReading.objects.get()
        self.client.force_login(get_user_model().objects.create_user('intruder'))
        self.assertEqual(self.client.post(reverse('rentals:reading_void', args=[reading.pk]), {'reason': 'x'}).status_code, 404)
