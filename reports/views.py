"""Reports for landlords: monthly summary per property, CSV exports and printable documents."""

import csv
import datetime
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from payments.models import MpesaAccount, Payment
from rentals.models import Lease, Property, RentCharge, Tenant
from rentals.views import statement

# Money that isn't income: deposits held and returned, and credits.
NON_INCOME_KINDS = [
    RentCharge.Kind.DEPOSIT, RentCharge.Kind.DEPOSIT_APPLIED, RentCharge.Kind.REFUND,
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


@login_required
def overview(request):
    user = request.user
    month = _month(request.GET.get('month'))
    next_month = _add_months(month, 1)

    properties = list(Property.objects.filter(owner=user).prefetch_related('units'))
    billed = dict(
        _billed(RentCharge.objects.filter(lease__unit__property__owner=user, period=month))
        .values_list('lease__unit__property').annotate(total=Sum('amount'))
    )
    collected = dict(
        Payment.objects.filter(
            lease__unit__property__owner=user,
            paid_at__date__gte=month, paid_at__date__lt=next_month,
        ).values_list('lease__unit__property').annotate(total=Sum('amount'))
    )
    arrears, occupied = defaultdict(Decimal), defaultdict(int)
    for lease in Lease.objects.filter(unit__property__owner=user, is_active=True).select_related('unit'):
        occupied[lease.unit.property_id] += 1
        arrears[lease.unit.property_id] += max(lease.balance, Decimal('0'))

    rows = []
    for prop in properties:
        b, c = billed.get(prop.pk) or Decimal('0'), collected.get(prop.pk) or Decimal('0')
        rows.append({
            'property': prop, 'units': len(prop.units.all()), 'occupied': occupied[prop.pk],
            'billed': b, 'collected': c, 'arrears': arrears[prop.pk],
            'rate': min(100, round(c * 100 / b)) if b else None,
        })
    totals = {k: sum(r[k] for r in rows) for k in ('units', 'occupied', 'billed', 'collected', 'arrears')}
    totals['rate'] = min(100, round(totals['collected'] * 100 / totals['billed'])) if totals['billed'] else None

    # Twelve-month trend ending at the selected month.
    start = _add_months(month, -11)
    billed_by_month = dict(
        _billed(RentCharge.objects.filter(lease__unit__property__owner=user, period__gte=start, period__lte=month))
        .values_list('period').annotate(total=Sum('amount'))
    )
    collected_by_month = {
        timezone.localdate(m) if isinstance(m, datetime.datetime) else m: t
        for m, t in Payment.objects.filter(
            lease__unit__property__owner=user, paid_at__date__gte=start, paid_at__date__lt=next_month,
        ).annotate(m=TruncMonth('paid_at')).values_list('m').annotate(total=Sum('amount'))
    }
    trend = []
    for i in range(12):
        m = _add_months(start, i)
        trend.append({'month': m, 'billed': billed_by_month.get(m) or 0, 'collected': collected_by_month.get(m) or 0})
    peak = max([max(t['billed'], t['collected']) for t in trend] + [1])
    for t in trend:
        t['billed_pct'] = round(t['billed'] * 100 / peak)
        t['collected_pct'] = round(t['collected'] * 100 / peak)

    return render(request, 'reports/overview.html', {
        'month': month, 'prev_month': _add_months(month, -1),
        'next_month': next_month if next_month <= datetime.date.today() else None,
        'rows': rows, 'totals': totals, 'trend': trend,
    })


def _money(value):
    return f'{Decimal(value or 0):.2f}'


def _csv_response(filename):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.write('﻿')  # BOM so Excel opens UTF-8 correctly
    return response


@login_required
def rent_roll_csv(request):
    """Every active lease with rent, deposit held and current balance."""
    response = _csv_response(f'rent-roll-{datetime.date.today():%Y-%m-%d}.csv')
    writer = csv.writer(response)
    writer.writerow(['Property', 'Unit', 'Tenant', 'Phone', 'Lease start', 'Monthly rent', 'Service charge',
                     'Deposit held', 'Balance', 'Last payment date', 'Last payment amount', 'Account number'])
    leases = (Lease.objects.filter(unit__property__owner=request.user, is_active=True)
              .select_related('tenant', 'unit__property'))
    for lease in leases:
        last = lease.payments.order_by('-paid_at').first()
        writer.writerow([
            lease.unit.property.name, lease.unit.label, lease.tenant.full_name, lease.tenant.phone,
            lease.start_date.isoformat(), _money(lease.monthly_rent), _money(lease.service_charge),
            _money(lease.deposit_held), _money(lease.balance),
            timezone.localtime(last.paid_at).strftime('%Y-%m-%d') if last else '',
            _money(last.amount) if last else '', lease.account_number,
        ])
    return response


@login_required
def payments_csv(request):
    month = request.GET.get('month')
    payments = (Payment.objects.filter(lease__unit__property__owner=request.user)
                .select_related('lease__tenant', 'lease__unit__property').order_by('paid_at'))
    if month:
        start = _month(month)
        payments = payments.filter(paid_at__date__gte=start, paid_at__date__lt=_add_months(start, 1))
    response = _csv_response(f'payments-{month or "all"}.csv')
    writer = csv.writer(response)
    writer.writerow(['Date', 'Property', 'Unit', 'Tenant', 'Method', 'Reference', 'Amount', 'Notes'])
    for p in payments:
        writer.writerow([
            timezone.localtime(p.paid_at).strftime('%Y-%m-%d %H:%M'), p.lease.unit.property.name,
            p.lease.unit.label, p.lease.tenant.full_name, p.get_method_display(), p.reference, _money(p.amount),
            p.notes,
        ])
    return response


# --- Printable documents (landlord and tenant versions share templates) -----------------

def _landlord_details(owner):
    account = MpesaAccount.objects.filter(owner=owner).first()
    return {
        'landlord_name': owner.get_full_name() or owner.username,
        'landlord_email': owner.email,
        'mpesa_account': account if account and account.is_complete() else None,
    }


def render_statement(request, lease, back_url):
    return render(request, 'reports/print_statement.html', {
        'lease': lease, 'entries': statement(lease, limit=None)[::-1], 'balance': lease.balance,
        'generated': timezone.localtime(), 'back_url': back_url, **_landlord_details(lease.unit.property.owner),
    })


def render_receipt(request, payment, back_url):
    return render(request, 'reports/print_receipt.html', {
        'payment': payment, 'lease': payment.lease, 'balance_after': payment.lease.balance,
        'back_url': back_url, **_landlord_details(payment.lease.unit.property.owner),
    })


@login_required
def lease_statement(request, pk):
    lease = get_object_or_404(Lease.objects.select_related('tenant', 'unit__property'),
                              pk=pk, unit__property__owner=request.user)
    return render_statement(request, lease, reverse('rentals:lease_detail', args=[pk]))


@login_required
def payment_receipt(request, pk):
    payment = get_object_or_404(Payment.objects.select_related('lease__tenant', 'lease__unit__property'),
                                pk=pk, lease__unit__property__owner=request.user)
    return render_receipt(request, payment, reverse('rentals:lease_detail', args=[payment.lease_id]))


def tenant_statement(request, token, lease_pk):
    tenant = get_object_or_404(Tenant, portal_token=token)
    lease = get_object_or_404(tenant.leases.select_related('unit__property'), pk=lease_pk)
    return render_statement(request, lease, reverse('rentals:tenant_portal', args=[token]))


def tenant_receipt(request, token, payment_pk):
    tenant = get_object_or_404(Tenant, portal_token=token)
    payment = get_object_or_404(Payment.objects.select_related('lease__unit__property'),
                                pk=payment_pk, lease__tenant=tenant)
    return render_receipt(request, payment, reverse('rentals:tenant_portal', args=[token]))
