"""Celebrations, the collection scorecard, the year in review and dashboard suggestions."""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from payments.models import Payment
from rentals.models import Lease, Property, RentCharge, Tenant, Unit
from rentals.tests import make_lease

from .highlights import fully_paid, nudges, rent_months, scorecard, year_in_review
from .utils import _add_months

D = Decimal
TODAY = datetime.date.today()
MONTH = TODAY.replace(day=1)


def at(day, hour=10):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour)))


def charge(lease, period, amount='10000', day=1):
    return RentCharge.objects.create(lease=lease, period=period, due_date=period.replace(day=day),
                                     amount=D(amount), kind=RentCharge.Kind.RENT)


def pay(lease, day, amount='10000'):
    return Payment.objects.create(lease=lease, amount=D(amount), method='cash', paid_at=at(day))


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', first_name='Grace')
        self.owner.date_joined = timezone.now() - datetime.timedelta(days=800)
        self.owner.save()
        self.prop = Property.objects.create(owner=self.owner, name='Riverside')
        self.prop.created_at = timezone.now() - datetime.timedelta(days=800)
        self.prop.save()

    def lease(self, label, start=None, rent='10000', **kwargs):
        unit = Unit.objects.create(property=self.prop, label=label, default_rent=D(rent))
        tenant = Tenant.objects.create(owner=self.owner, full_name=f'Jane {label}', phone='0712345678')
        return Lease.objects.create(unit=unit, tenant=tenant, start_date=start or _add_months(MONTH, -12),
                                    monthly_rent=D(rent), **kwargs)


class OnTimeTests(Base):
    def test_on_time_and_late_months(self):
        lease = self.lease('A1')
        m1, m2 = _add_months(MONTH, -3), _add_months(MONTH, -2)
        charge(lease, m1)
        charge(lease, m2)
        pay(lease, m1 + datetime.timedelta(days=2))   # within the grace days
        pay(lease, m2 + datetime.timedelta(days=12))  # late
        rows = rent_months(lease)
        self.assertEqual([on_time for _, _, on_time in rows], [True, False])
        self.assertEqual(rows[1][1], m2 + datetime.timedelta(days=12))


class CelebrationTests(Base):
    def test_fully_paid_property_is_celebrated(self):
        a, b = self.lease('A1'), self.lease('A2')
        for lease in (a, b):
            charge(lease, MONTH, day=min(28, max(TODAY.day, 1)))
        pay(a, TODAY)
        self.assertEqual(fully_paid(self.owner), [])  # A2 hasn't paid
        pay(b, TODAY)
        [item] = fully_paid(self.owner)
        self.assertEqual(item['property'], self.prop)
        self.assertEqual(item['paid_on'], TODAY)

        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), f'Riverside is fully paid for {MONTH:%B}')

    def test_evening_summary_celebrates_the_day_it_happened(self):
        from notifications.utils import payment_summary

        a = self.lease('A1')
        charge(a, MONTH, day=min(28, TODAY.day))
        pay(a, TODAY)
        self.assertIn('fully paid', payment_summary(self.owner, TODAY)[3])


class ScorecardTests(Base):
    def month_at(self, lease, n, collected):
        period = _add_months(MONTH, n)
        charge(lease, period)
        if collected:
            pay(lease, period + datetime.timedelta(days=1), collected)

    def test_best_since_and_streak(self):
        lease = self.lease('A1')
        for n, collected in ((-4, '5000'), (-3, '10000'), (-2, '9500'), (-1, '9500')):
            self.month_at(lease, n, collected)
        self.month_at(lease, 0, '10000')
        card = scorecard(self.owner)
        self.assertEqual(card['rate'], 100)
        self.assertEqual(card['best_since'], _add_months(MONTH, -3))
        self.assertEqual(card['streak'], 3)  # -1, -2, -3 at 90%+; -4 was 50%

        self.client.force_login(self.owner)
        page = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(page, f'your best since {_add_months(MONTH, -3):%B}')
        self.assertContains(page, '3 months in a row above 90%')

    def test_nothing_to_show_for_new_landlords(self):
        self.assertIsNone(scorecard(self.owner))


class YearInReviewTests(Base):
    def test_numbers_and_page(self):
        year = TODAY.year if TODAY.month > 3 else TODAY.year - 1
        a, b = self.lease('A1', start=datetime.date(year, 1, 1)), self.lease('A2', start=datetime.date(year, 1, 1))
        for m in (1, 2, 3):
            period = datetime.date(year, m, 1)
            charge(a, period)
            charge(b, period)
            pay(a, period)
            pay(b, period + datetime.timedelta(days=15))
        review = year_in_review(self.owner, year)
        self.assertEqual(review['collected'], D('60000'))
        self.assertEqual(review['reliable']['lease'], a)
        self.assertEqual(review['reliable']['on_time'], 3)

        self.client.force_login(self.owner)
        page = self.client.get(reverse('reports:year', args=[year]))
        self.assertContains(page, 'KES 60,000')
        self.assertContains(page, 'Jane A1')
        self.assertContains(page, 'data-share=')
        self.assertEqual(self.client.get(reverse('reports:year', args=[TODAY.year + 1])).status_code, 404)

    def test_dashboard_offers_it_in_december_to_february(self):
        from .highlights import review_year

        self.assertEqual(review_year(datetime.date(2026, 12, 5)), 2026)
        self.assertEqual(review_year(datetime.date(2027, 1, 20)), 2026)
        self.assertIsNone(review_year(datetime.date(2027, 6, 1)))


class NudgeTests(Base):
    def test_long_empty_unit(self):
        self.lease('A1', rent='12000')  # let: sets the going rate for single rooms
        empty = Unit.objects.create(property=self.prop, label='B3', default_rent=D('11000'))
        old = Lease.objects.create(unit=empty, tenant=Tenant.objects.create(owner=self.owner, full_name='Old'),
                                   start_date=_add_months(MONTH, -12), monthly_rent=D('11000'), is_active=False,
                                   end_date=TODAY - datetime.timedelta(days=45))
        self.assertTrue(old.pk)
        [nudge] = nudges(self.owner)
        self.assertIn('B3 at Riverside has been empty 45 days', nudge['text'])
        self.assertIn('KES 12,000', nudge['text'])
        self.assertEqual(nudge['url'], reverse('listings:create', args=[empty.pk]))

        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), 'has been empty 45 days')
        self.client.post(reverse('reports:nudge_dismiss'), {'key': nudge['key']})
        self.assertEqual(nudges(self.owner), [])

    def test_late_three_months_running(self):
        lease = self.lease('A1')
        for n in (-3, -2, -1):
            period = _add_months(MONTH, n)
            charge(lease, period)
            pay(lease, period + datetime.timedelta(days=15), '9000')
        [nudge] = nudges(self.owner)
        self.assertIn('Jane (A1) has paid late 3 months running', nudge['text'])
        self.assertEqual(nudge['url'], reverse('rentals:plan_create', args=[lease.pk]))

    def test_good_payers_get_no_nudge(self):
        lease = make_lease(self.owner, start=_add_months(MONTH, -4))
        for n in (-3, -2, -1):
            period = _add_months(MONTH, n)
            charge(lease, period)
            pay(lease, period)
        self.assertFalse([n for n in nudges(self.owner) if n['key'].startswith('late')])
