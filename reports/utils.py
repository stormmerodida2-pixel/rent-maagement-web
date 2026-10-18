"""Helpers for reports: month arithmetic, what counts as income, and the monthly summary."""

import datetime
from decimal import Decimal

from django.db.models import Sum

from maintenance.models import MaintenanceRequest
from payments.models import Payment
from rentals.models import Lease, RentCharge, Unit


# --- Months and billing rules (shared with the Reports page) -------------------------------------------

# Money that isn't income: deposits held and returned, and credits.
NON_INCOME_KINDS = [
    RentCharge.Kind.DEPOSIT, RentCharge.Kind.DEPOSIT_APPLIED, RentCharge.Kind.REFUND,
    RentCharge.Kind.OPENING,  # balances carried over when a landlord starts using Rentipay
    RentCharge.Kind.ADJUSTMENT,  # corrections to what's owed, not new rent
]


def _month(value):
    try:
        return datetime.datetime.strptime(value or '', '%Y-%m').date()
    except ValueError:
        return datetime.date.today().replace(day=1)


def _add_months(day, n):
    month = day.month - 1 + n
    return day.replace(year=day.year + month // 12, month=month % 12 + 1, day=1)


def _billed(charges):
    return charges.exclude(kind__in=NON_INCOME_KINDS).filter(amount__gt=0)


# --- Monthly summary ---------------------------------------------------------------------------------

# A landlord's month in numbers, for the monthly report card (same rules as the Reports page).

def month_summary(user, month):
    """Totals for the month starting `month` (a date on the 1st), plus how things stand today."""
    next_month = _add_months(month, 1)

    def billed_in(start):
        return _billed(RentCharge.objects.filter(lease__unit__property__owner=user, period=start)) \
            .aggregate(t=Sum('amount'))['t'] or Decimal('0')

    def collected_in(start, end):
        return Payment.objects.filter(
            lease__unit__property__owner=user, paid_at__date__gte=start, paid_at__date__lt=end,
        ).aggregate(t=Sum('amount'))['t'] or Decimal('0')

    billed, collected = billed_in(month), collected_in(month, next_month)
    previous = _add_months(month, -1)
    prev_collected = collected_in(previous, month)

    owing = []
    leases = Lease.objects.filter(unit__property__owner=user, is_active=True).select_related('tenant', 'unit')
    for lease in leases:
        balance = lease.balance
        if balance > 0:
            owing.append((lease, balance))
    owing.sort(key=lambda row: row[1], reverse=True)
    units = Unit.objects.filter(property__owner=user).count()

    return {
        'month': month,
        'billed': billed,
        'collected': collected,
        'rate': min(100, round(collected * 100 / billed)) if billed else None,
        'prev_collected': prev_collected,
        'change': collected - prev_collected,
        'arrears': sum(balance for _, balance in owing),
        'owing_count': len(owing),
        'top_owing': owing[:3],
        'units': units,
        'occupied': len(leases),
        'vacant': units - len(leases),
        'open_repairs': MaintenanceRequest.objects.filter(property__owner=user).exclude(
            status__in=[MaintenanceRequest.Status.RESOLVED, MaintenanceRequest.Status.CANCELLED]).count(),
        'had_activity': bool(billed or collected),
    }


# --- Receipt codes (anyone can check a receipt is genuine at /verify/) --------------------------------------------

def receipt_code(payment):
    """e.g. "R4F-7KQ2X": the payment number in base 36 plus a check that can't be guessed."""
    import base64
    import hashlib
    import hmac

    from django.conf import settings

    from accounts.utils import base36

    check = hmac.new(settings.SECRET_KEY.encode(), f'receipt:{payment.pk}'.encode(), hashlib.sha256).digest()
    return f'R{base36(payment.pk).upper()}-{base64.b32encode(check).decode()[:5]}'


def payment_from_code(code):
    """The payment (voided ones included) a receipt code belongs to, or None."""
    import hmac

    code = ''.join((code or '').split()).upper()
    ident, _, _check = code.removeprefix('R').partition('-')
    try:
        pk = int(ident, 36)
    except ValueError:
        return None
    payment = Payment.all_objects.select_related('lease__tenant', 'lease__unit__property').filter(pk=pk).first()
    if payment is None or not hmac.compare_digest(receipt_code(payment), code):
        return None
    return payment
