"""The encouraging parts of the dashboard: fully-paid celebrations, the collection scorecard and streaks, the
year in review, and nudges suggesting what to do next."""

import datetime
from decimal import Decimal
from statistics import median

from django.db.models import Sum
from django.utils import timezone

from maintenance.models import MaintenanceRequest
from payments.models import Payment
from rentals.models import Lease, PaymentPlan, Property, RentCharge, Unit

from .utils import _add_months, _billed

ON_TIME_GRACE = 5  # days after the rent day still counted as on time (or the lease's own grace, if longer)
STREAK_RATE = 90   # % collected for a month to count towards a streak
VACANT_NUDGE_DAYS = 30
LATE_RUN = 3       # rent months in a row paid late before suggesting a payment plan


# --- When each month's rent was paid ---------------------------------------------------------------------------

def rent_months(lease, charges=None, payments=None):
    """[(rent charge, day it was fully covered or None, on time?)] oldest first.

    Payments settle the oldest charges first, so a month counts as covered once everything billed up to its rent
    day has been paid. On time means covered within ON_TIME_GRACE days of the rent day."""
    charges = sorted(charges if charges is not None else lease.charges.all(), key=lambda c: (c.due_date, c.pk))
    payments = sorted(payments if payments is not None else lease.payments.all(), key=lambda p: p.paid_at)
    grace = datetime.timedelta(days=max(ON_TIME_GRACE, lease.late_fee_grace_days or 0))
    rows, owed = [], Decimal('0')
    for charge in charges:
        owed += charge.amount
        if charge.kind != RentCharge.Kind.RENT:
            continue
        paid, covered = Decimal('0'), None
        for p in payments:
            paid += p.amount
            if paid >= owed:
                covered = p.paid_at.date()
                break
        if covered is None and owed <= 0:
            covered = charge.due_date  # in credit already
        rows.append((charge, covered, covered is not None and covered <= charge.due_date + grace))
    return rows


# --- Fully paid this month ---------------------------------------------------------------------------------------

def fully_paid(owner, today=None):
    """Properties where every tenant has paid this month's rent: [{'property', 'month', 'paid_on', 'days_early'}]."""
    today = today or datetime.date.today()
    month = today.replace(day=1)
    found = []
    for prop in Property.objects.filter(owner=owner):
        leases = list(Lease.objects.filter(unit__property=prop, is_active=True))
        this_month = []
        for lease in leases:
            rows = [r for r in rent_months(lease) if r[0].period == month]
            if rows:
                this_month.append(rows[-1])
        if not this_month or len(this_month) < len(leases) or any(covered is None for _, covered, _ in this_month):
            continue
        if any(lease.balance > 0 for lease in leases):
            continue
        # Fully paid "for October" from when October's rent was billed, even if someone paid ahead.
        paid_on = max(max(covered, timezone.localtime(charge.created_at).date()) for charge, covered, _ in this_month)
        last_due = max(charge.due_date for charge, _, _ in this_month)
        found.append({'property': prop, 'month': month, 'paid_on': paid_on,
                      'days_early': (last_due - paid_on).days})
    return found


def celebration_text(item):
    when = item['days_early']
    timing = (f', {when} day{"s" if when != 1 else ""} early' if when > 0 else ', right on time' if when == 0
              else '')
    return f'🎉 {item["property"].name} is fully paid for {item["month"]:%B}{timing}!'


# --- Collection scorecard and streaks -----------------------------------------------------------------------------

def monthly_rates(owner, months=13, today=None):
    """[(month, billed, collected, rate %)] oldest first, ending with this month (so far)."""
    today = today or datetime.date.today()
    this_month = today.replace(day=1)
    rows = []
    for n in range(months - 1, -1, -1):
        start = _add_months(this_month, -n)
        end = _add_months(start, 1)
        billed = _billed(RentCharge.objects.filter(lease__unit__property__owner=owner, period=start)) \
            .aggregate(t=Sum('amount'))['t'] or Decimal('0')
        collected = Payment.objects.filter(lease__unit__property__owner=owner, paid_at__date__gte=start,
                                           paid_at__date__lt=end).aggregate(t=Sum('amount'))['t'] or Decimal('0')
        rate = min(100, round(collected * 100 / billed)) if billed else None
        rows.append((start, billed, collected, rate))
    return rows


def scorecard(owner, today=None):
    """This month's collection rate with how it compares, and the run of strong months before it."""
    rows = monthly_rates(owner, today=today)
    month, _, _, rate = rows[-1]
    past = rows[:-1]
    if rate is None and not any(r[3] for r in past):
        return None
    best_since = None
    if rate is not None and past and past[-1][3] is not None and rate > past[-1][3]:
        earlier = [r for r in past if r[3] is not None and r[3] >= rate]
        best_since = earlier[-1][0] if earlier else 'year'
    streak = 0
    for _, _, _, r in reversed(past):
        if r is None or r < STREAK_RATE:
            break
        streak += 1
    return {
        'month': month, 'rate': rate, 'best_since': best_since, 'streak': streak, 'streak_rate': STREAK_RATE,
        'bars': [{'month': m, 'rate': r or 0, 'none': r is None} for m, _, _, r in rows[-6:]],
    }


# --- Year in review -----------------------------------------------------------------------------------------------

def year_in_review(owner, year):
    start, end = datetime.date(year, 1, 1), datetime.date(year + 1, 1, 1)
    months = [r for r in monthly_rates(owner, months=24, today=min(end - datetime.timedelta(days=1),
                                                                     datetime.date.today()))
              if r[0].year == year]
    collected = sum((r[2] for r in months), Decimal('0'))
    billed = sum((r[1] for r in months), Decimal('0'))
    best = max(months, key=lambda r: r[2], default=None)

    reliable = None
    for lease in Lease.objects.filter(unit__property__owner=owner).select_related('tenant', 'unit__property'):
        rows = [r for r in rent_months(lease) if start <= r[0].due_date < end and r[0].due_date <= datetime.date.today()]
        if len(rows) < 3:
            continue
        on_time = sum(1 for r in rows if r[2])
        score = (on_time / len(rows), len(rows))
        if reliable is None or score > reliable['score']:
            reliable = {'lease': lease, 'on_time': on_time, 'months': len(rows), 'score': score}

    from payments.models import Expense

    spent = Expense.objects.filter(owner=owner, paid_on__gte=start, paid_on__lt=end) \
        .aggregate(t=Sum('amount'))['t'] or Decimal('0')
    return {
        'year': year,
        'collected': collected,
        'billed': billed,
        'rate': min(100, round(collected * 100 / billed)) if billed else None,
        'best_month': best if best and best[2] > 0 else None,
        'reliable': reliable,
        'repairs_fixed': MaintenanceRequest.objects.filter(property__owner=owner, resolved_at__date__gte=start,
                                                           resolved_at__date__lt=end).count(),
        'moved_in': Lease.objects.filter(unit__property__owner=owner, start_date__gte=start,
                                         start_date__lt=end).count(),
        'spent': spent,
        'made': collected - spent,
        'units': Unit.objects.filter(property__owner=owner).count(),
        'occupied': Lease.objects.filter(unit__property__owner=owner, is_active=True).count(),
        'payments': Payment.objects.filter(lease__unit__property__owner=owner, paid_at__date__gte=start,
                                           paid_at__date__lt=end).count(),
    }


def review_year(today=None):
    """The year to offer a review of on the dashboard: this year in December, last year in January and February."""
    today = today or datetime.date.today()
    if today.month == 12:
        return today.year
    if today.month <= 2:
        return today.year - 1
    return None


# --- Nudges ------------------------------------------------------------------------------------------------------

def nudges(owner, today=None, limit=5):
    """Suggestions for what to do next: [{'key', 'text', 'action', 'url'}]."""
    from django.urls import reverse

    from listings.models import Listing

    from .models import DismissedNudge

    today = today or datetime.date.today()
    hidden = set(DismissedNudge.objects.filter(owner=owner, until__gte=today).values_list('key', flat=True))
    found = []

    # Empty for a month or more, and not advertised.
    active = {lease.unit_id: lease for lease in Lease.objects.filter(unit__property__owner=owner, is_active=True)
              .select_related('unit', 'tenant')}
    rents = {}
    for lease in active.values():
        rents.setdefault(lease.unit.unit_type, []).append(lease.monthly_rent)
    listed = set(Listing.objects.filter(unit__property__owner=owner, is_open=True).values_list('unit_id', flat=True))
    for unit in Unit.objects.filter(property__owner=owner).select_related('property'):
        if unit.pk in active or unit.pk in listed:
            continue
        last_end = unit.leases.filter(is_active=False).order_by('-end_date').values_list('end_date', flat=True).first()
        since = last_end or unit.property.created_at.date()
        days = (today - since).days
        if days < VACANT_NUDGE_DAYS:
            continue
        similar = rents.get(unit.unit_type)
        price = (f' Your other {unit.get_unit_type_display().lower()}s rent for KES {median(similar):,.0f}.'
                 if similar else '')
        found.append({'key': f'vacant-{unit.pk}', 'days': days,
                      'text': f'{unit.label} at {unit.property.name} has been empty {days} days.{price}',
                      'action': 'List it', 'url': reverse('listings:create', args=[unit.pk])})

    # Late several months running and still owing, with no payment plan.
    planned = set(PaymentPlan.objects.filter(lease__unit__property__owner=owner, status=PaymentPlan.Status.ACTIVE)
                  .values_list('lease_id', flat=True))
    for lease in active.values():
        if lease.pk in planned:
            continue
        rows = [r for r in rent_months(lease) if r[0].due_date + datetime.timedelta(days=ON_TIME_GRACE) < today]
        if len(rows) < LATE_RUN or any(on_time for _, _, on_time in rows[-LATE_RUN:]) or lease.balance <= 0:
            continue
        name = lease.tenant.full_name.split()[0] if lease.tenant.full_name else 'This tenant'
        found.append({'key': f'late-{lease.pk}', 'days': 0,
                      'text': f'{name} ({lease.unit.label}) has paid late {LATE_RUN} months running and owes '
                              f'KES {lease.balance:,.0f}. A payment plan could help.',
                      'action': 'Set up a plan', 'url': reverse('rentals:plan_create', args=[lease.pk])})

    found = [n for n in found if n['key'] not in hidden]
    found.sort(key=lambda n: -n['days'])
    return found[:limit]
