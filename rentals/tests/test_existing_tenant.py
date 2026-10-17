"""Moving in a tenant who already lived there before Rentipay, with what they owe (or have in credit)."""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..models import Lease, Property, RentCharge, Tenant, Unit

D = Decimal
TODAY = datetime.date.today()


class ExistingTenantTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        prop = Property.objects.create(owner=self.owner, name='Riverside')
        self.unit = Unit.objects.create(property=prop, label='A1', default_rent=15000)
        self.tenant = Tenant.objects.create(owner=self.owner, full_name='Jane Wanjiru', phone='254712345678')
        self.client.force_login(self.owner)

    def move_in(self, **data):
        return self.client.post(reverse('rentals:lease_create'), {
            'unit': self.unit.pk, 'tenant': self.tenant.pk, 'start_date': '2024-03-05', 'monthly_rent': '',
            'service_charge': '', 'deposit': '', 'due_day': '', 'late_fee': '', 'late_fee_grace_days': '',
            **data})

    def test_existing_tenant_with_arrears(self):
        resp = self.move_in(tenancy='existing', balance_now='22000', deposit_paid='15000')
        lease = Lease.objects.get()
        self.assertRedirects(resp, reverse('rentals:lease_detail', args=[lease.pk]))
        self.assertEqual(lease.balance, D('22000'))  # exactly what they owe, nothing billed twice
        self.assertEqual(lease.deposit, D('15000'))
        opening = RentCharge.objects.get(kind=RentCharge.Kind.OPENING, amount__gt=0)
        self.assertEqual(opening.description, 'Balance brought forward')
        self.assertTrue(RentCharge.objects.filter(kind=RentCharge.Kind.DEPOSIT, amount=D('15000')).exists())
        self.assertFalse(RentCharge.objects.filter(kind=RentCharge.Kind.RENT).exists())  # this month: in the balance
        self.assertEqual(lease.due_day, 5)  # rent day from when they moved in

        # Next month bills normally.
        next_month = (TODAY.replace(day=1) + datetime.timedelta(days=32)).replace(day=10)
        lease.ensure_charge_for(next_month)
        self.assertEqual(lease.balance, D('37000'))
        page = self.client.get(reverse('rentals:lease_detail', args=[lease.pk]))
        self.assertContains(page, 'Balance brought forward')

    def test_existing_tenant_in_credit(self):
        self.move_in(tenancy='existing', balance_now='-2000')
        lease = Lease.objects.get()
        self.assertEqual(lease.balance, D('-2000'))
        self.assertEqual(RentCharge.objects.get(kind=RentCharge.Kind.OPENING).description, 'Credit brought forward')

    def test_existing_tenant_paid_up(self):
        self.move_in(tenancy='existing', balance_now='0')
        self.assertEqual(Lease.objects.get().balance, D('0'))

    def test_existing_tenant_moved_in_in_the_past(self):
        future = (TODAY + datetime.timedelta(days=5)).isoformat()
        resp = self.move_in(tenancy='existing', start_date=future, balance_now='0')
        self.assertContains(resp, 'the day they moved in')
        self.assertFalse(Lease.objects.exists())

    def test_new_tenant_unchanged(self):
        self.move_in(tenancy='new', start_date=TODAY.isoformat(), deposit='15000', bill_now='on', bill_deposit='on')
        lease = Lease.objects.get()
        self.assertFalse(RentCharge.objects.filter(kind=RentCharge.Kind.OPENING).exists())
        self.assertEqual(lease.balance, D('30000'))  # this month's rent + deposit

    def test_form_shows_the_choice(self):
        page = self.client.get(reverse('rentals:lease_create'))
        self.assertContains(page, 'Already living here (before Rentipay)')
        self.assertContains(page, 'data-show-when="id_tenancy=existing"')
