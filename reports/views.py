"""Reports for landlords: monthly summary per property, CSV exports and printable documents."""

import csv
import datetime
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponse
from django.conf import settings
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from payments.models import Expense, MpesaAccount, Payment
from payments.utils import expenses_by_property
from rentals.utils import tenant_language
from rentals.models import Lease, Property, RentCharge, Tenant
from rentals.views import statement

from .utils import _add_months, _billed, _month

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

    spent = expenses_by_property(user, month, next_month)
    rows = []
    for prop in properties:
        b, c = billed.get(prop.pk) or Decimal('0'), collected.get(prop.pk) or Decimal('0')
        s = spent.get(prop.pk) or Decimal('0')
        rows.append({
            'property': prop, 'units': len(prop.units.all()), 'occupied': occupied[prop.pk],
            'billed': b, 'collected': c, 'arrears': arrears[prop.pk], 'spent': s, 'net': c - s,
            'rate': min(100, round(c * 100 / b)) if b else None,
        })
    shared = spent.get(None) or Decimal('0')  # costs for all properties together
    totals = {k: sum(r[k] for r in rows) for k in ('units', 'occupied', 'billed', 'collected', 'arrears', 'spent')}
    totals['spent'] += shared
    totals['net'] = totals['collected'] - totals['spent']
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
    spent_by_month = {
        timezone.localdate(m) if isinstance(m, datetime.datetime) else m: t
        for m, t in Expense.objects.filter(owner=user, paid_on__gte=start, paid_on__lt=next_month)
        .annotate(m=TruncMonth('paid_on')).values_list('m').annotate(total=Sum('amount'))
    }
    trend = []
    for i in range(12):
        m = _add_months(start, i)
        c, s = collected_by_month.get(m) or 0, spent_by_month.get(m) or 0
        trend.append({'month': m, 'billed': billed_by_month.get(m) or 0, 'collected': c, 'spent': s, 'net': c - s})
    peak = max([max(t['billed'], t['collected'], t['spent']) for t in trend] + [1])
    for t in trend:
        t['billed_pct'] = round(t['billed'] * 100 / peak)
        t['collected_pct'] = round(t['collected'] * 100 / peak)
        t['spent_pct'] = round(t['spent'] * 100 / peak)

    return render(request, 'reports/overview.html', {
        'month': month, 'prev_month': _add_months(month, -1),
        'next_month': next_month if next_month <= datetime.date.today() else None,
        'rows': rows, 'totals': totals, 'trend': trend, 'shared': shared,
        'year_net': sum(t['net'] for t in trend),
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
        'mpesa_account': account if account and account.has_pay_number else None,
    }


# Receipts and statements are documents for the tenant, so they're always in the tenant's language,
# even when the landlord prints one. Only the toolbar (Back / Print) follows whoever is viewing.

def render_statement(request, lease, back_url, viewer_language='en'):
    with tenant_language(lease.tenant):
        return render(request, 'reports/print_statement.html', {
            'lease': lease, 'entries': statement(lease, limit=None)[::-1], 'balance': lease.balance,
            'generated': timezone.localtime(), 'back_url': back_url, 'viewer_language': viewer_language,
            **_landlord_details(lease.unit.property.owner),
        })


def render_receipt(request, payment, back_url, viewer_language='en'):
    with tenant_language(payment.lease.tenant):
        return render(request, 'reports/print_receipt.html', {
            'payment': payment, 'lease': payment.lease, 'balance_after': payment.lease.balance,
            'back_url': back_url, 'viewer_language': viewer_language,
            **_landlord_details(payment.lease.unit.property.owner),
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
    return render_statement(request, lease, reverse('rentals:tenant_portal', args=[token]), tenant.language)


def tenant_receipt(request, token, payment_pk):
    tenant = get_object_or_404(Tenant, portal_token=token)
    payment = get_object_or_404(Payment.objects.select_related('lease__tenant', 'lease__unit__property'),
                                pk=payment_pk, lease__tenant=tenant)
    return render_receipt(request, payment, reverse('rentals:tenant_portal', args=[token]), tenant.language)


# --- Year in review and dashboard suggestions -----------------------------------------------------------------------

@login_required
def year_review(request, year):
    from .highlights import year_in_review

    today = datetime.date.today()
    first = request.user.date_joined.year
    if not first <= year <= today.year:
        from django.http import Http404

        raise Http404
    review = year_in_review(request.user, year)
    share = (f'My {year} with {settings.APP_NAME}: KES {review["collected"]:,.0f} rent collected'
             + (f' ({review["rate"]}% of what was due)' if review['rate'] is not None else '')
             + f', {review["repairs_fixed"]} repairs fixed.')
    return render(request, 'reports/year_review.html', {
        'r': review, 'share_text': share, 'so_far': year == today.year,
        'years': list(range(today.year, first - 1, -1)),
    })


@login_required
@require_POST
def nudge_dismiss(request):
    from .models import DismissedNudge

    key = request.POST.get('key', '')[:40]
    if key:
        DismissedNudge.objects.update_or_create(owner=request.user, key=key, defaults={
            'until': datetime.date.today() + datetime.timedelta(days=30)})
    return redirect('rentals:dashboard')
