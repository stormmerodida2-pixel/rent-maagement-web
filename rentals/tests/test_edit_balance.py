"""Edit rent & details: "Balance owed now" corrects what the tenant owes with one "Balance adjustment" line."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEntry
from reports.utils import _billed

from ..models import RentCharge
from . import make_lease

D = Decimal


class EditBalanceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord')
        self.lease = make_lease(self.owner)
        RentCharge.objects.create(lease=self.lease, period=self.lease.start_date.replace(day=1),
                                  due_date=self.lease.start_date, amount=D('15000'), kind=RentCharge.Kind.RENT)
        self.client.force_login(self.owner)
        self.url = reverse('rentals:lease_update', args=[self.lease.pk])

    def post(self, **changes):
        data = {'unit': self.lease.unit_id, 'tenant': self.lease.tenant_id,
                'start_date': self.lease.start_date.isoformat(), 'monthly_rent': '15000', 'service_charge': '0',
                'deposit': '0', 'due_day': str(self.lease.due_day), 'late_fee': '0', 'late_fee_grace_days': '3',
                'balance_owed': '15000', 'balance_reason': ''}
        data.update(changes)
        return self.client.post(self.url, data, follow=True)

    def test_field_shows_the_current_balance(self):
        form = self.client.get(self.url).context['form']
        self.assertEqual(form.fields['balance_owed'].initial, D('15000'))

    def test_lowering_it(self):
        page = self.post(balance_owed='0', balance_reason='October was actually paid')
        self.assertContains(page, 'Balance updated')
        self.assertEqual(self.lease.balance, D('0'))
        line = RentCharge.objects.get(kind=RentCharge.Kind.ADJUSTMENT)
        self.assertEqual((line.amount, line.description), (D('-15000'), 'October was actually paid'))
        self.assertContains(page, 'October was actually paid')  # on the statement
        entry = AuditEntry.objects.filter(action='lease.edited').latest('created_at')
        self.assertIn('Balance owed now (KES)', entry.changes)

    def test_raising_it_and_credit(self):
        self.post(balance_owed='18000')
        self.assertEqual(self.lease.balance, D('18000'))
        self.post(balance_owed='-500')
        self.assertEqual(self.lease.balance, D('-500'))
        self.assertEqual(RentCharge.objects.filter(kind=RentCharge.Kind.ADJUSTMENT).count(), 2)

    def test_unchanged_means_no_adjustment(self):
        self.post(monthly_rent='16000')
        self.assertFalse(RentCharge.objects.filter(kind=RentCharge.Kind.ADJUSTMENT).exists())
        self.lease.refresh_from_db()
        self.assertEqual(self.lease.monthly_rent, D('16000'))

    def test_adjustments_are_not_rent_income(self):
        self.post(balance_owed='20000')
        self.assertFalse(_billed(RentCharge.objects.filter(kind=RentCharge.Kind.ADJUSTMENT)).exists())

    def test_voiding_it_undoes_it(self):
        self.post(balance_owed='0')
        line = RentCharge.objects.get(kind=RentCharge.Kind.ADJUSTMENT)
        line.void(self.owner, 'Mistake')
        self.assertEqual(self.lease.balance, D('15000'))
