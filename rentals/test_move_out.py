"""Month-to-month tenancies: recording a tenant's notice to move out."""

import datetime

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from accounts.verification import mark_verified
from audit.models import AuditEntry
from notifications.move_out import send_move_out_reminders

from .models import Lease
from .tests import make_lease

TODAY = datetime.date.today()


class MoveOutNoticeTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', email='l@example.com', password='pw')
        mark_verified(self.owner)
        self.lease = make_lease(self.owner, label='A1', start=TODAY.replace(day=1) - datetime.timedelta(days=200))
        self.client.force_login(self.owner)
        self.url = reverse('rentals:lease_notice', args=[self.lease.pk])

    def give_notice(self, move_out, **extra):
        return self.client.post(self.url, {'move_out': move_out.isoformat(), 'given_on': TODAY.isoformat(), **extra})

    def test_new_leases_are_month_to_month(self):
        self.assertNotContains(self.client.get(reverse('rentals:lease_create')), 'name="end_date"')

    def test_defaults_to_the_end_of_a_rent_month(self):
        self.assertContains(self.client.get(self.url), f'value="{self.lease.default_move_out().isoformat()}"')

    def test_record_notice_shows_everywhere_and_stops_billing_after(self):
        move_out = self.lease.default_move_out()
        resp = self.give_notice(move_out, note='Moving to Nakuru')
        self.assertRedirects(resp, reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.lease.refresh_from_db()
        self.assertEqual((self.lease.end_date, self.lease.notice_given_on), (move_out, TODAY))
        self.assertTrue(self.lease.is_active)  # still living there until they move out
        badge = f'Moving out {move_out:%d %b}'
        self.assertContains(self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk])), 'Moving to Nakuru')
        self.assertContains(self.client.get(reverse('rentals:lease_list')), badge)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), 'Moving out soon')
        portal = self.client.get(reverse('rentals:tenant_portal', args=[self.lease.tenant.portal_token]))
        self.assertContains(portal, "moving out on")
        # Billing: the rent month ending on the move-out date is billed; the next one isn't.
        next_rent_day = move_out + datetime.timedelta(days=1)
        last_month = next_rent_day.replace(day=1) - datetime.timedelta(days=1)
        self.assertTrue(self.lease.ensure_charge_for(last_month)[0])
        self.assertIsNone(self.lease.ensure_charge_for(next_rent_day)[0])
        self.assertTrue(AuditEntry.objects.filter(action='lease.notice_given').exists())
        # Finishing the move-out starts from the agreed date.
        end_page = self.client.get(reverse('rentals:lease_end', args=[self.lease.pk]))
        self.assertContains(end_page, f'value="{move_out.isoformat()}"')

    def test_cancel_notice(self):
        self.give_notice(self.lease.default_move_out())
        self.client.post(self.url, {'action': 'cancel'})
        self.lease.refresh_from_db()
        self.assertIsNone(self.lease.end_date)
        self.assertFalse(self.lease.moving_out)
        self.assertTrue(AuditEntry.objects.filter(action='lease.notice_cancelled').exists())

    def test_past_dates_point_to_end_lease(self):
        resp = self.give_notice(TODAY - datetime.timedelta(days=1))
        self.assertContains(resp, 'use &quot;End lease&quot; instead')

    def test_overdue_move_out_prompts_to_finish(self):
        Lease.objects.filter(pk=self.lease.pk).update(end_date=TODAY - datetime.timedelta(days=2))
        page = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(page, 'Finish move-out')

    def test_other_landlords_cannot(self):
        other = make_lease(get_user_model().objects.create_user('other'), label='Z9')
        self.assertEqual(self.client.get(reverse('rentals:lease_notice', args=[other.pk])).status_code, 404)

    def test_reminder_once_a_few_days_before(self):
        Lease.objects.filter(pk=self.lease.pk).update(end_date=TODAY + datetime.timedelta(days=2))
        self.assertEqual(send_move_out_reminders(), 1)
        self.assertIn('moves out of A1', mail.outbox[0].subject)
        self.assertIn('Finish move-out', mail.outbox[0].body)
        self.assertEqual(send_move_out_reminders(), 0)  # only once
        # Too early: nothing yet.
        later = make_lease(self.owner, label='A2')
        Lease.objects.filter(pk=later.pk).update(end_date=TODAY + datetime.timedelta(days=20))
        self.assertEqual(send_move_out_reminders(), 0)
