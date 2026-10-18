"""Paying rent made rewarding: the tenant's on-time record and reference letter, the landlord's on-time reward,
and a thank-you in the receipt."""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from notifications.models import SmsMessage
from payments.models import Payment
from reports.utils import _add_months

from ..models import Lease, Property, RentCharge, RewardGiven, RewardRule, Tenant, Unit
from ..rewards import give_rewards, praise, reference_token, tenant_record

D = Decimal
TODAY = datetime.date.today()
MONTH = TODAY.replace(day=1)


def at(day):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(10)))


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', first_name='Grace', last_name='Wanjiru')
        prop = Property.objects.create(owner=self.owner, name='Kodi Apartments')
        unit = Unit.objects.create(property=prop, label='A1', default_rent=D('10000'))
        self.tenant = Tenant.objects.create(owner=self.owner, full_name='Jane Akinyi', phone='0712345678')
        self.lease = Lease.objects.create(unit=unit, tenant=self.tenant, start_date=_add_months(MONTH, -12),
                                          monthly_rent=D('10000'))

    def month(self, n, late_days=0, amount='10000'):
        """Bill rent `n` months from now (due on the 1st) and pay it `late_days` after the rent day."""
        period = _add_months(MONTH, n)
        RentCharge.objects.create(lease=self.lease, period=period, due_date=period, amount=D('10000'),
                                  kind=RentCharge.Kind.RENT)
        if amount:
            Payment.objects.create(lease=self.lease, amount=D(amount), method='cash',
                                   paid_at=at(period + datetime.timedelta(days=late_days)))


class RecordTests(Base):
    def test_record_and_badge(self):
        for n in range(-8, 0):
            self.month(n, late_days=15 if n == -8 else 1)
        record = tenant_record(self.tenant)
        self.assertEqual((record['on_time'], record['months'], record['streak']), (7, 8, 7))
        self.assertTrue(record['good'])

        page = self.client.get(reverse('rentals:tenant_portal', args=[self.tenant.portal_token]))
        self.assertContains(page, 'Paid on time 7 of 8 months')
        self.assertContains(page, 'Good tenant')
        self.assertContains(page, '7 months on time in a row')

    def test_reference_letter(self):
        for n in range(-3, 0):
            self.month(n)
        resp = self.client.get(reverse('rentals:tenant_reference_link', args=[self.tenant.portal_token]))
        letter = self.client.get(resp['Location'])
        self.assertContains(letter, 'Jane Akinyi')
        self.assertContains(letter, 'Grace Wanjiru')
        self.assertContains(letter, 'Paid rent on time 3 of the last 3 months')
        self.assertContains(letter, 'Owes no rent today')

        self.assertEqual(self.client.get(reverse('rentals:tenant_reference', args=['made-up'])).status_code, 404)
        old = reference_token(self.tenant)
        self.tenant.portal_token = 'a-brand-new-portal-token'
        self.tenant.save()
        self.assertEqual(self.client.get(reverse('rentals:tenant_reference', args=[old])).status_code, 404)


class RewardTests(Base):
    def rule(self, **kwargs):
        return RewardRule.objects.create(owner=self.owner, months=3, amount=D('500'),
                                         started_on=_add_months(MONTH, -6), **kwargs)

    def test_earned_after_a_run_of_on_time_months(self):
        self.rule()
        self.month(-5, late_days=20)  # late: doesn't count
        for n in (-4, -3, -2):
            self.month(n)
        self.assertEqual(give_rewards(), 1)
        reward = RewardGiven.objects.get()
        self.assertEqual(reward.through_period, _add_months(MONTH, -2))
        self.assertEqual(reward.charge.amount, D('-500'))
        self.assertEqual(self.lease.balance, D('-500'))
        sms = SmsMessage.objects.get()
        self.assertIn('on time 3 months in a row', sms.body)
        self.assertEqual(give_rewards(), 0)  # once per run

    def test_a_late_month_starts_again(self):
        self.rule()
        self.month(-4)
        self.month(-3)
        self.month(-2, late_days=20)
        self.month(-1)
        self.assertEqual(give_rewards(), 0)

    def test_only_months_after_it_started_count(self):
        RewardRule.objects.create(owner=self.owner, months=3, amount=D('500'), started_on=TODAY)
        for n in (-4, -3, -2):
            self.month(n)
        self.assertEqual(give_rewards(), 0)

    def test_tenant_sees_progress(self):
        self.rule()
        self.month(-2)
        page = self.client.get(reverse('rentals:tenant_portal', args=[self.tenant.portal_token]))
        self.assertContains(page, 'get KES 500 off your rent')
        self.assertContains(page, '1 of 3 done')

    def test_landlord_sets_it_up(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('accounts:settings')), 'On-time rewards')
        self.client.post(reverse('accounts:rewards'), {'is_active': 'on', 'months': '6', 'amount': '500'})
        rule = RewardRule.objects.get()
        self.assertEqual((rule.months, rule.amount, rule.started_on), (6, D('500'), timezone.localdate()))


class ThankYouTests(Base):
    def test_early_payment_is_praised(self):
        period = _add_months(MONTH, 1)
        RentCharge.objects.create(lease=self.lease, period=period, due_date=period, amount=D('10000'),
                                  kind=RentCharge.Kind.RENT)
        payment = Payment.objects.create(lease=self.lease, amount=D('10000'), method='cash',
                                         paid_at=at(period - datetime.timedelta(days=3)))
        self.assertEqual(praise(payment), 'Well done for paying early!')

    def test_streak_mentioned(self):
        for n in (-3, -2, -1):
            self.month(n)
        self.month(0)
        payment = Payment.objects.latest('paid_at')
        text = praise(payment)
        self.assertIn('Well done for paying on time!', text)
        self.assertIn('4 months on time in a row', text)

    def test_no_praise_when_still_owing(self):
        self.month(-1, amount='4000')
        self.assertEqual(praise(Payment.objects.get()), '')

    def test_receipt_includes_it(self):
        from notifications.utils import cash_receipt_text

        self.month(-1)
        payment = Payment.objects.get()
        self.assertIn('Well done for paying on time!', cash_receipt_text(payment, self.lease.balance, 'Peter'))
