"""Planning rent changes for month-to-month leases (one lease, or a whole property at once)."""

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from audit import log as audit
from notifications.models import SmsMessage
from notifications.sms import rent_change_text, send_personal_sms

from .models import RentChange


def new_rent_for(old, mode, value):
    """The new rent for one lease: 'percent' (+10 → 10% more), 'amount' (+1000) or 'set' (20000)."""
    if mode == 'percent':
        new = old * (1 + value / Decimal('100'))
    elif mode == 'amount':
        new = old + value
    else:
        new = value
    # Whole shillings, rounded to the nearest 50 for percentage rises (KES 18,900 not 18,937.50).
    step = Decimal('50') if mode == 'percent' else Decimal('1')
    return max((new / step).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * step, Decimal('1'))


def plan(leases_and_rents, effective_from, *, notify, actor):
    """Create (or replace) the pending change on each lease. Returns the changes, plus SMS if notified."""
    changes = []
    with transaction.atomic():
        for lease, new_rent in leases_and_rents:
            RentChange.objects.filter(lease=lease, applied_at__isnull=True).delete()
            change = RentChange.objects.create(lease=lease, old_rent=lease.monthly_rent, new_rent=new_rent,
                                               effective_from=effective_from)
            audit.record(owner=actor, actor=actor, action='lease.rent_change_planned', lease=lease,
                         summary=f'Rent for {lease.tenant} changes from KES {change.old_rent:,.0f} to '
                                 f'KES {change.new_rent:,.0f} from {change.starts_on:%d %b %Y}.')
            changes.append(change)
    messages = []
    if notify and changes:
        messages = send_personal_sms([(c.lease, c.lease.tenant.phone, rent_change_text(c)) for c in changes],
                                     kind=SmsMessage.Kind.OTHER)
        sent = {m.lease_id for m in messages if m.status != SmsMessage.Status.FAILED}
        RentChange.objects.filter(pk__in=[c.pk for c in changes if c.lease_id in sent]).update(notified_at=timezone.now())
    return changes, messages


def cancel(change, *, actor):
    lease = change.lease
    change.delete()
    audit.record(owner=actor, actor=actor, action='lease.rent_change_cancelled', lease=lease,
                 summary=f'Cancelled the planned rent change for {lease.tenant}.')
