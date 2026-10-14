"""Sending announcements: once only, one delivery per tenant, SMS in bulk."""

import logging
from collections import Counter, defaultdict

from django.db import transaction
from django.utils import timezone

from audit import log as audit
from notifications.models import SmsMessage
from notifications.sms import send_bulk_sms

from .models import Announcement, Delivery

logger = logging.getLogger(__name__)


def send(announcement):
    """Send a draft or due scheduled announcement. Safe to call twice: only the first call sends.

    Returns a Counter of SMS outcomes ('sent', 'failed', 'logged') plus 'recipients'.
    """
    with transaction.atomic():
        a = Announcement.objects.select_for_update().get(pk=announcement.pk)
        if a.status not in (Announcement.Status.DRAFT, Announcement.Status.SCHEDULED):
            return Counter()
        # Marked sent before any message goes out, so a crash or a second click can't send twice.
        a.status, a.sent_at = Announcement.Status.SENT, timezone.now()
        a.save(update_fields=['status', 'sent_at'])

    leases = a.recipient_leases()
    Delivery.objects.bulk_create(
        [Delivery(announcement=a, tenant=lease.tenant, lease=lease) for lease in leases], ignore_conflicts=True,
    )
    stats = Counter(recipients=len(leases))
    if a.send_sms and leases:
        # One bulk send per version, so Kiswahili readers get the Kiswahili text when there is one.
        by_version = defaultdict(list)
        for lease in leases:
            by_version[a.version_for(lease.tenant.language)].append(lease)
        messages = []
        for version, group in by_version.items():
            messages += send_bulk_sms([(lease, lease.tenant.phone) for lease in group], a.sms_text(version),
                                      kind=SmsMessage.Kind.ANNOUNCEMENT)
        by_lease = {m.lease_id: m for m in messages}
        deliveries = list(a.deliveries.all())
        for delivery in deliveries:
            delivery.sms = by_lease.get(delivery.lease_id)
        Delivery.objects.bulk_update(deliveries, ['sms'])
        stats.update(m.status for m in messages)

    audit.record(
        owner=a.owner, actor=None if a.scheduled_for else a.owner, action='announcement.sent',
        summary=f'Sent announcement "{a.title}" to {len(leases)} tenant(s)'
                + (f"; SMS: {stats['sent'] + stats['logged']} sent, {stats['failed']} failed." if a.send_sms else '.'),
    )
    logger.info('Announcement %s sent to %s tenants', a.pk, len(leases))
    return stats


def send_due(now=None):
    """Send scheduled announcements whose time has come. Returns how many were sent."""
    now = now or timezone.now()
    due = Announcement.objects.filter(status=Announcement.Status.SCHEDULED, scheduled_for__lte=now)
    count = 0
    for announcement in due:
        if send(announcement):
            count += 1
    return count
