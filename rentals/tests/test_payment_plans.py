"""Payment plans: clearing arrears in agreed instalments, with reminders and progress tracking."""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEntry
from notifications.models import SmsMessage
from notifications.utils import send_plan_reminders
from payments.models import Payment

from ..models import PaymentPlan, RentCharge
from ..utils import create_plan, on_track_plan, plan_progress, plan_schedule, refresh_plan
from . import make_lease

TODAY = datetime.date.today()
DAY = datetime.timedelta(days=1)
WEEK = datetime.timedelta(days=7)


def owe(lease, amount, due=datetime.date(2026, 3, 15)):
    return RentCharge.objects.create(lease=lease, period=due.replace(day=1), amount=amount, due_date=due)


class ScheduleTests(TestCase):
    def test_last_payment_is_whatever_is_left(self):
        rows = plan_schedule(Decimal('45000'), Decimal('10000'), 'weekly', datetime.date(2026, 10, 16))
        self.assertEqual([a for _, a in rows], [Decimal('10000')] * 4 + [Decimal('5000')])
        self.assertEqual(rows[-1][0], datetime.date(2026, 11, 13))

    def test_fortnightly_and_monthly(self):
        rows = plan_schedule(Decimal('20000'), Decimal('10000'), 'fortnightly', datetime.date(2026, 10, 16))
        self.assertEqual(rows[1][0], datetime.date(2026, 10, 30))
        rows = plan_schedule(Decimal('30000'), Decimal('10000'), 'monthly', datetime.date(2027, 1, 31))
        self.assertEqual([d for d, _ in rows], [datetime.date(2027, 1, 31), datetime.date(2027, 2, 28),
                                                datetime.date(2027, 3, 31)])


class PlanProgressTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner)
        owe(self.lease, Decimal('45000'))
        self.plan = create_plan(self.lease, total=Decimal('45000'), instalment=Decimal('10000'), frequency='weekly',
                                first_due=TODAY - WEEK, note='', actor=self.owner)

    def pay(self, amount):
        return Payment.objects.create(lease=self.lease, amount=amount, method=Payment.Method.CASH)

    def test_missed_instalment_means_behind(self):
        progress = plan_progress(self.plan, TODAY)
        self.assertEqual(progress['missed'], 1)
        self.assertFalse(progress['on_track'])
        self.assertEqual([r['state'] for r in progress['rows'][:2]], ['missed', 'due'])
        self.assertIsNone(on_track_plan(self.lease))

    def test_paying_keeps_them_on_track(self):
        self.pay(Decimal('10000'))
        progress = plan_progress(self.plan, TODAY)
        self.assertEqual(progress['paid'], Decimal('10000'))
        self.assertTrue(progress['on_track'])
        self.assertEqual(progress['next'].due_date, TODAY)
        self.assertEqual(on_track_plan(self.lease), self.plan)

    def test_new_rent_is_paid_before_the_plan_moves_on(self):
        owe(self.lease, Decimal('15000'), due=TODAY - DAY)  # billed after the plan started
        self.pay(Decimal('15000'))
        self.assertEqual(plan_progress(self.plan, TODAY)['paid'], Decimal('0'))
        self.pay(Decimal('10000'))
        self.assertEqual(plan_progress(self.plan, TODAY)['paid'], Decimal('10000'))

    def test_part_payment(self):
        self.pay(Decimal('4000'))
        progress = plan_progress(self.plan, TODAY)
        self.assertEqual(progress['rows'][0]['state'], 'part')
        self.assertEqual(progress['missed'], 1)

    def test_cleared_plan_completes(self):
        self.pay(Decimal('45000'))
        progress = refresh_plan(self.plan, TODAY)
        self.assertTrue(progress['done'])
        self.assertEqual(progress['percent'], 100)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, PaymentPlan.Status.COMPLETED)
        self.assertIsNotNone(self.plan.ended_at)

    def test_payments_before_the_plan_dont_count(self):
        Payment.objects.create(lease=self.lease, amount=Decimal('5000'), method=Payment.Method.CASH,
                               paid_at=timezone.now() - datetime.timedelta(days=30))
        self.assertEqual(plan_progress(self.plan, TODAY)['paid'], Decimal('0'))


class PlanReminderTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner)
        owe(self.lease, Decimal('30000'))

    def make_plan(self, first_due):
        return create_plan(self.lease, total=Decimal('30000'), instalment=Decimal('10000'), frequency='weekly',
                           first_due=first_due, note='', actor=self.owner)

    def reminders(self):
        return SmsMessage.objects.filter(lease=self.lease, kind=SmsMessage.Kind.REMINDER)

    def test_reminded_two_days_before_once(self):
        first_due = TODAY + 3 * DAY
        self.make_plan(first_due)
        self.assertEqual(send_plan_reminders(first_due - 3 * DAY), 0)
        self.assertEqual(send_plan_reminders(first_due - 2 * DAY), 1)
        self.assertEqual(send_plan_reminders(first_due - DAY), 0)
        self.assertIn('KES 10,000', self.reminders().get().body)

    def test_missed_amount_is_rolled_into_the_next_reminder(self):
        plan = self.make_plan(TODAY - 5 * DAY)
        self.assertEqual(send_plan_reminders(TODAY), 1)  # the second one is due in 2 days
        self.assertIn('KES 20,000', self.reminders().get().body)
        self.assertIsNotNone(plan.instalments.all()[1].reminded_on)

    def test_no_reminders_once_cancelled(self):
        plan = self.make_plan(TODAY + 2 * DAY)
        plan.status = PaymentPlan.Status.CANCELLED
        plan.save()
        self.assertEqual(send_plan_reminders(TODAY), 0)


class PlanPagesTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner)
        owe(self.lease, Decimal('45000'))
        self.client.force_login(self.owner)
        self.url = reverse('rentals:plan_create', args=[self.lease.pk])
        self.data = {'total': '45000', 'instalment': '10000', 'frequency': 'weekly',
                     'first_due': (TODAY + 2 * DAY).isoformat(), 'note': '', 'notify': 'on'}

    def test_set_up_button_on_tenant_page(self):
        page = self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.assertContains(page, 'Set up payment plan')

    def test_form_defaults_to_the_balance(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'value="45000')

    def test_preview_then_agree(self):
        preview = self.client.post(self.url, self.data)
        self.assertContains(preview, '5 payments')
        self.assertContains(preview, 'Agree plan')
        self.assertFalse(PaymentPlan.objects.exists())

        resp = self.client.post(self.url, {**self.data, 'save': '1'})
        self.assertRedirects(resp, reverse('rentals:lease_detail', args=[self.lease.pk]))
        plan = PaymentPlan.objects.get()
        self.assertEqual(plan.instalments.count(), 5)
        sms = SmsMessage.objects.get(lease=self.lease)
        self.assertIn('5 payments of KES 10,000 every week', sms.body)
        self.assertTrue(AuditEntry.objects.filter(action='lease.plan_created').exists())

        page = self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.assertContains(page, 'On track')
        self.assertNotContains(page, 'Set up payment plan')

    def test_rejects_bad_plans(self):
        resp = self.client.post(self.url, {**self.data, 'first_due': (TODAY - DAY).isoformat()})
        self.assertFalse(resp.context['form'].is_valid())
        resp = self.client.post(self.url, {**self.data, 'instalment': '50000'})
        self.assertFalse(resp.context['form'].is_valid())

    def test_cancel(self):
        self.client.post(self.url, {**self.data, 'save': '1', 'notify': ''})
        plan = PaymentPlan.objects.get()
        self.client.post(reverse('rentals:plan_cancel', args=[plan.pk]))
        plan.refresh_from_db()
        self.assertEqual(plan.status, PaymentPlan.Status.CANCELLED)
        self.assertFalse(SmsMessage.objects.exists())

    def test_list_dashboard_and_remind_all(self):
        self.client.post(self.url, {**self.data, 'save': '1', 'notify': ''})
        self.assertContains(self.client.get(reverse('rentals:plan_list')), self.lease.tenant.full_name)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), 'Payment plans')
        remind = self.client.get(reverse('rentals:remind_all'))
        self.assertContains(remind, 'On a payment plan')
        self.assertEqual(remind.context['selected_count'], 0)

    def test_tenant_sees_their_plan_in_their_language(self):
        self.client.post(self.url, {**self.data, 'save': '1', 'notify': ''})
        tenant = self.lease.tenant
        tenant.language = 'sw'
        tenant.save()
        page = self.client.get(reverse('rentals:tenant_portal', args=[tenant.portal_token]))
        self.assertContains(page, 'Mpango wako wa malipo')

    def test_private(self):
        self.client.post(self.url, {**self.data, 'save': '1', 'notify': ''})
        plan = PaymentPlan.objects.get()
        self.client.force_login(get_user_model().objects.create_user('other'))
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(reverse('rentals:plan_cancel', args=[plan.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse('rentals:plan_list')), self.lease.tenant.full_name)
