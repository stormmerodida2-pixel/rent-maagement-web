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


class MonthByMonthTests(TestCase):
    """Moved in on a date: every month since is listed, and the landlord marks which were paid."""

    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        prop = Property.objects.create(owner=self.owner, name='Riverside')
        self.unit = Unit.objects.create(property=prop, label='A1', default_rent=10000)
        self.tenant = Tenant.objects.create(owner=self.owner, full_name='Jane Wanjiru', phone='254712345678')
        self.client.force_login(self.owner)
        # Moved in on the 5th, four months ago: four or five rent months have started since.
        start = (TODAY.replace(day=1) - datetime.timedelta(days=100)).replace(day=5)
        self.base = {'tenancy': 'existing', 'owed_how': 'months', 'unit': self.unit.pk, 'tenant': self.tenant.pk,
                     'start_date': start.isoformat(), 'monthly_rent': '', 'service_charge': '', 'deposit': '',
                     'due_day': '', 'late_fee': '', 'late_fee_grace_days': '', 'deposit_paid': '10000'}

    def test_lists_every_month_then_records_the_unpaid_ones(self):
        step2 = self.client.post(reverse('rentals:lease_create'), self.base)
        self.assertContains(step2, 'Which months did Jane Wanjiru pay?')
        months = step2.context['months']
        self.assertGreaterEqual(len(months), 4)
        self.assertEqual(months[0]['start'].day, 5)
        self.assertFalse(Lease.objects.exists())  # nothing saved yet

        last = len(months) - 1
        choices = {f'month_{m["index"]}': 'paid' for m in months}
        choices.update({f'month_{last - 1}': 'unpaid', f'month_{last}': 'part', f'month_{last}_paid': '4000'})
        resp = self.client.post(reverse('rentals:lease_create'), {**self.base, **choices, 'months_confirmed': '1'})
        lease = Lease.objects.get()
        self.assertRedirects(resp, reverse('rentals:lease_detail', args=[lease.pk]))
        self.assertEqual(lease.balance, D('16000'))  # one month unpaid + 6,000 left of the last
        lines = list(RentCharge.objects.filter(kind=RentCharge.Kind.OPENING, amount__gt=0).order_by('due_date'))
        self.assertEqual([c.amount for c in lines], [D('10000'), D('6000')])
        self.assertTrue(lines[0].label.startswith('Unpaid rent '))
        self.assertFalse(RentCharge.objects.filter(kind=RentCharge.Kind.RENT).exists())  # not billed twice

        page = self.client.get(reverse('rentals:lease_detail', args=[lease.pk]))
        self.assertContains(page, "What's unpaid")
        self.assertContains(page, '(KES 4,000 of 10,000 paid)')
        unpaid = page.context['unpaid']
        self.assertEqual([owed for _, owed in unpaid], [D('10000'), D('6000')])
        self.assertNotIn(RentCharge.Kind.DEPOSIT, [c.kind for c, _ in unpaid])  # paid before Rentipay: not owed

    def test_part_paid_needs_a_sensible_amount(self):
        months = self.client.post(reverse('rentals:lease_create'), self.base).context['months']
        resp = self.client.post(reverse('rentals:lease_create'), {**self.base, 'months_confirmed': '1',
                                                                  f'month_{months[0]["index"]}': 'part',
                                                                  f'month_{months[0]["index"]}_paid': '99999'})
        self.assertContains(resp, 'enter how much they paid')
        self.assertFalse(Lease.objects.exists())

    def test_one_total_still_works(self):
        self.client.post(reverse('rentals:lease_create'), {**self.base, 'owed_how': 'total', 'balance_now': '7000'})
        self.assertEqual(Lease.objects.get().balance, D('7000'))


class UnpaidBreakdownTests(TestCase):
    def test_payments_settle_the_oldest_first(self):
        from payments.models import Payment

        from ..tests import make_lease
        from ..utils import unpaid_items

        owner = get_user_model().objects.create_user('landlord')
        lease = make_lease(owner, start=TODAY.replace(day=1) - datetime.timedelta(days=70))
        for month in (lease.start_date, TODAY - datetime.timedelta(days=35), TODAY):
            lease.ensure_charge_for(month)
        charges = list(lease.charges.order_by('due_date'))
        Payment.objects.create(lease=lease, amount=charges[0].amount + D('5000'), method='cash')
        items = unpaid_items(lease)
        self.assertEqual(items[0][0], charges[1])
        self.assertEqual(items[0][1], charges[1].amount - D('5000'))
        self.assertEqual(sum(owed for _, owed in items), lease.balance)


class TenantSeesWhatTheyOweTests(TestCase):
    def test_rent_page_lists_unpaid_months_in_their_language(self):
        owner = get_user_model().objects.create_user('landlord')
        prop = Property.objects.create(owner=owner, name='Riverside')
        unit = Unit.objects.create(property=prop, label='A1', default_rent=10000)
        tenant = Tenant.objects.create(owner=owner, full_name='Jane', phone='254712345678', language='sw')
        lease = Lease.objects.create(unit=unit, tenant=tenant, start_date=TODAY - datetime.timedelta(days=90),
                                     monthly_rent=10000, due_day=5)
        from ..utils import bring_forward, rent_months

        months = rent_months(lease)
        bring_forward(lease, D('0'), D('0'), owner, unpaid_months=[(months[0], D('10000')), (months[1], D('4000'))])
        page = self.client.get(reverse('rentals:tenant_portal', args=[tenant.portal_token]))
        self.assertContains(page, 'Kodi ambayo haijalipwa')
        self.assertContains(page, 'KES 14,000')


class EditMonthsTests(TestCase):
    """A month marked "Not paid" by mistake (the tenant had paid it) is corrected, and the balance follows."""

    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        prop = Property.objects.create(owner=self.owner, name='Riverside')
        self.unit = Unit.objects.create(property=prop, label='A1', default_rent=10000)
        self.tenant = Tenant.objects.create(owner=self.owner, full_name='Jane Wanjiru', phone='254712345678')
        self.client.force_login(self.owner)
        start = (TODAY.replace(day=1) - datetime.timedelta(days=100)).replace(day=5)
        base = {'tenancy': 'existing', 'owed_how': 'months', 'unit': self.unit.pk, 'tenant': self.tenant.pk,
                'start_date': start.isoformat(), 'monthly_rent': '', 'service_charge': '', 'deposit': '',
                'due_day': '', 'late_fee': '', 'late_fee_grace_days': '', 'deposit_paid': ''}
        self.months = self.client.post(reverse('rentals:lease_create'), base).context['months']
        choices = {f'month_{m["index"]}': 'paid' for m in self.months}
        choices.update({'month_0': 'unpaid', 'month_1': 'part', 'month_1_paid': '4000'})  # the mistake: month 0
        self.client.post(reverse('rentals:lease_create'), {**base, **choices, 'months_confirmed': '1'})
        self.lease = Lease.objects.get()
        self.url = reverse('rentals:edit_opening_months', args=[self.lease.pk])

    def test_page_shows_what_was_chosen(self):
        self.assertEqual(self.lease.balance, D('16000'))
        self.assertContains(self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk])), self.url)
        months = self.client.get(self.url).context['months']
        self.assertEqual([m['status'] for m in months[:2]], ['unpaid', 'part'])
        self.assertEqual(months[1]['paid_text'], '4000')
        self.assertTrue(all(m['status'] == 'paid' for m in months[2:]))

    def test_mark_it_paid_and_the_balance_follows(self):
        choices = {f'month_{m["index"]}': 'paid' for m in self.months}
        choices.update({'month_1': 'part', 'month_1_paid': '4000'})  # month 0 was actually paid
        resp = self.client.post(self.url, choices)
        self.assertRedirects(resp, reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.assertEqual(self.lease.balance, D('6000'))
        # The old lines are kept, voided, so the history shows the correction.
        self.assertEqual(RentCharge.all_objects.filter(kind=RentCharge.Kind.OPENING, voided_at__isnull=False).count(), 2)
        from audit.models import AuditEntry

        self.assertTrue(AuditEntry.objects.filter(action='opening.corrected').exists())
        again = self.client.get(self.url).context['months']
        self.assertEqual([m['status'] for m in again[:2]], ['paid', 'part'])

    def test_and_back_again(self):
        choices = {f'month_{m["index"]}': 'unpaid' for m in self.months}
        self.client.post(self.url, choices)
        self.assertEqual(self.lease.balance, D('10000') * len(self.months))

    def test_bad_part_amount_changes_nothing(self):
        resp = self.client.post(self.url, {'month_0': 'part', 'month_0_paid': '99999'})
        self.assertContains(resp, 'enter how much they paid')
        self.assertEqual(self.lease.balance, D('16000'))

    def test_new_tenants_have_nothing_to_fix(self):
        unit = Unit.objects.create(property=self.unit.property, label='A2', default_rent=10000)
        tenant = Tenant.objects.create(owner=self.owner, full_name='New Person', phone='254700000000')
        new = Lease.objects.create(unit=unit, tenant=tenant, start_date=TODAY, monthly_rent=10000)
        resp = self.client.get(reverse('rentals:edit_opening_months', args=[new.pk]))
        self.assertRedirects(resp, reverse('rentals:lease_detail', args=[new.pk]), fetch_redirect_response=False)
