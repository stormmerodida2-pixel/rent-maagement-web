"""A tenant's whole story with the landlord on one timeline: payments, repairs, notes, messages, texts and rewards."""

import datetime

from django.urls import reverse
from django.utils import timezone

KINDS = [('payment', 'Payments'), ('repair', 'Repairs'), ('note', 'Notes'), ('message', 'Messages'),
         ('sms', 'Texts'), ('tenancy', 'Moves')]


def _at(day):
    """Dates (move-in, move-out) sort among timestamps at the start of that day."""
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


def tenant_timeline(tenant, kinds=None, limit=200):
    """[{'kind', 'when', 'title', 'detail', 'url', 'tone'}] newest first, for every tenancy this tenant had."""
    from maintenance.models import MaintenanceRequest
    from notifications.models import SmsMessage
    from payments.models import Payment
    from support.models import Complaint

    from .models import RewardGiven, TenantNote

    leases = list(tenant.leases.select_related('unit__property'))
    events = []

    def add(kind, when, title, detail='', url='', tone=''):
        events.append({'kind': kind, 'when': when, 'title': title, 'detail': detail, 'url': url, 'tone': tone})

    for lease in leases:
        add('tenancy', _at(lease.start_date), f'Moved into {lease.unit}',
            f'Rent KES {lease.monthly_rent:,.0f} a month', reverse('rentals:lease_detail', args=[lease.pk]))
        if lease.end_date and not lease.is_active:
            add('tenancy', _at(lease.end_date), f'Moved out of {lease.unit}', tone='muted')
        elif lease.end_date:
            add('tenancy', _at(lease.end_date), f'Due to move out of {lease.unit}', 'Notice given', tone='warn')

    for p in Payment.objects.filter(lease__in=leases).select_related('recorded_by'):
        by = f' · recorded by {p.recorded_by.get_full_name() or "you"}' if p.recorded_by_id else ''
        add('payment', p.paid_at, f'Paid KES {p.amount:,.0f}',
            f'{p.get_method_display()}{" " + p.reference if p.reference else ""}{by}',
            reverse('reports:payment_receipt', args=[p.pk]), 'good')

    for r in RewardGiven.objects.filter(lease__in=leases):
        add('payment', r.created_at, f'Earned an on-time reward: KES {r.amount:,.0f} off', tone='good')

    units = [lease.unit for lease in leases]
    for r in MaintenanceRequest.objects.filter(unit__in=units).prefetch_related('ratings'):
        add('repair', r.created_at, f'Repair logged: {r.title}',
            r.get_priority_display() + (' · reported by them' if r.reported_by_id == tenant.pk else ''),
            reverse('maintenance:update', args=[r.pk]))
        if r.resolved_at:
            rating = next((x for x in r.ratings.all() if x.tenant_id == tenant.pk), None)
            add('repair', r.resolved_at, f'Repair done: {r.title}',
                ('They rated it 👍' if rating.good else 'They rated it 👎') if rating else '',
                reverse('maintenance:update', args=[r.pk]), 'good')

    for n in TenantNote.objects.filter(lease__in=leases).select_related('author'):
        promise = (f' · promised KES {n.promised_amount:,.0f}' if n.promised_amount else '') + (
            f' by {n.promised_by:%d %b}' if n.promised_by else '')
        add('note', n.created_at, n.text, f'{(n.author.get_full_name() if n.author else "") or "Note"}{promise}')

    for c in Complaint.objects.filter(tenant=tenant):
        add('message', c.created_at, f'Message: {c.subject}', c.get_status_display(),
            reverse('support:complaint_detail', args=[c.pk]))

    for s in SmsMessage.objects.filter(lease__in=leases):
        add('sms', s.created_at, f'Text sent: {s.get_kind_display().lower()}', s.body[:120],
            tone='bad' if s.status == SmsMessage.Status.FAILED else '')

    if kinds:
        events = [e for e in events if e['kind'] in kinds]
    events.sort(key=lambda e: e['when'], reverse=True)
    return events[:limit]
