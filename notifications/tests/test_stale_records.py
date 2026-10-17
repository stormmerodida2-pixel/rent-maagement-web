"""Records not updated for a month: a dashboard warning, and a weekly reminder until the landlord updates them."""

import datetime

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit import utils as audit
from audit.models import AuditEntry
from notifications.models import LandlordPreferences, SmsMessage
from notifications.utils import is_stale, send_stale_reminders
from payments.models import MpesaAccount
from rentals.tests import make_lease

DAY = datetime.timedelta(days=1)


class StaleRecordsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', email='grace@example.com', first_name='Grace')
        from accounts.utils import mark_verified

        mark_verified(self.owner)
        self.lease = make_lease(self.owner)
        LandlordPreferences.objects.create(user=self.owner, phone='254700111222')
        self.now = timezone.now()
        self.age(40)

    def age(self, days):
        """Make the account, and everything recorded in it, `days` old."""
        then = self.now - days * DAY
        get_user_model().objects.filter(pk=self.owner.pk).update(date_joined=then)
        AuditEntry.objects.filter(owner=self.owner).update(created_at=then)
        self.owner.refresh_from_db()

    def test_reminded_after_a_month_then_weekly(self):
        self.assertEqual(is_stale(self.owner, self.now), 40)
        self.assertEqual(send_stale_reminders(self.now), 1)
        self.assertIn('records need updating', mail.outbox[-1].subject)
        self.assertIn('40 days', mail.outbox[-1].body)
        self.assertTrue(SmsMessage.objects.filter(phone='254700111222', body__contains='nothing recorded').exists())
        self.assertEqual(send_stale_reminders(self.now + 3 * DAY), 0)  # not again within the week
        self.assertEqual(send_stale_reminders(self.now + 7 * DAY), 1)

    def test_recent_updates_mean_no_reminder(self):
        audit.record(owner=self.owner, actor=self.owner, action='payment.recorded', summary='Cash')
        self.assertIsNone(is_stale(self.owner, self.now))
        self.assertEqual(send_stale_reminders(self.now), 0)

    def test_automatic_entries_do_not_count(self):
        audit.record(owner=self.owner, action='payment.received', summary='M-Pesa callback')  # no person
        self.assertEqual(send_stale_reminders(self.now), 1)

    def test_statement_upload_counts(self):
        MpesaAccount.objects.create(owner=self.owner, statement_imported_at=self.now - 2 * DAY)
        self.assertIsNone(is_stale(self.owner, self.now))

    def test_landlords_without_tenants_are_left_alone(self):
        self.lease.is_active = False
        self.lease.save()
        self.assertEqual(send_stale_reminders(self.now), 0)

    def test_dashboard_warns_and_confirming_clears_it(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(page, 'Nothing has been recorded for 40 days')
        self.client.post(reverse('rentals:records_confirmed'))
        self.assertTrue(AuditEntry.objects.filter(action='records.confirmed', actor=self.owner).exists())
        self.assertNotContains(self.client.get(reverse('rentals:dashboard')), 'Nothing has been recorded')
        self.assertEqual(send_stale_reminders(), 0)
