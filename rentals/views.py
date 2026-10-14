import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from accounts.views import setup_steps
from announcements.models import current_notices
from maintenance.forms import TenantMaintenanceForm
from maintenance.models import MaintenanceRequest
from notifications.sms import send_rent_reminder
from payments import mpesa
from payments.forms import StkPushForm
from payments.models import C2BPayment, MpesaAccount, Payment

from .i18n import tenant_page
from .models import Lease, RentCharge, Tenant, Unit


def home(request):
    """Public landing page."""
    return render(request, 'home.html')


def dashboard_numbers(user):
    """Everything the dashboard's live areas show (summary numbers, arrears, recent payments)."""
    today = datetime.date.today()
    month_start = today.replace(day=1)
    leases = (
        Lease.objects.filter(unit__property__owner=user, is_active=True)
        .select_related('tenant', 'unit__property')
    )
    lease_rows = [(lease, lease.balance) for lease in leases]
    arrears = [(lease, bal) for lease, bal in lease_rows if bal > 0]
    unit_count = Unit.objects.filter(property__owner=user).count()
    collected = (
        Payment.objects.filter(lease__unit__property__owner=user, paid_at__date__gte=month_start)
        .aggregate(total=Sum('amount'))['total']
        or 0
    )
    billed = (
        RentCharge.objects.filter(lease__unit__property__owner=user, period=month_start)
        .aggregate(total=Sum('amount'))['total']
        or 0
    )
    open_requests = (
        MaintenanceRequest.objects.filter(unit__property__owner=user)
        .exclude(status__in=[MaintenanceRequest.Status.RESOLVED, MaintenanceRequest.Status.CANCELLED])
        .select_related('unit__property')
        .order_by('-created_at')
    )
    return {
        'today': today,
        'open_requests': open_requests,
        'open_count': open_requests.count(),
        'urgent_count': open_requests.filter(
            priority__in=[MaintenanceRequest.Priority.HIGH, MaintenanceRequest.Priority.URGENT]
        ).count(),
        'unit_count': unit_count,
        'vacant_count': unit_count - len(lease_rows),
        'billed_this_month': billed,
        'collection_rate': min(100, round(collected * 100 / billed)) if billed else None,
        'occupied_count': len(lease_rows),
        'collected_this_month': collected,
        'total_arrears': sum(bal for _, bal in arrears),
        'arrears': sorted(arrears, key=lambda row: row[1], reverse=True),
        'recent_payments': Payment.objects.filter(lease__unit__property__owner=user)
        .select_related('lease__tenant', 'lease__unit')[:8],
    }


@login_required
def dashboard(request):
    user = request.user
    numbers = dashboard_numbers(user)
    steps = setup_steps(user)
    hour = timezone.localtime().hour
    greeting = 'Good morning' if hour < 12 else 'Good afternoon' if hour < 17 else 'Good evening'
    return render(request, 'rentals/dashboard.html', {
        **numbers,
        'greeting': greeting,
        'open_requests': numbers['open_requests'][:8],
        'unmatched_c2b': C2BPayment.objects.filter(account__owner=user, payment__isnull=True).count(),
        'mpesa_account': MpesaAccount.objects.filter(owner=user).first(),
        'setup_incomplete': not all(step['done'] for step in steps),
        'setup_done': sum(step['done'] for step in steps),
        'setup_total': len(steps),
        'setup_next': next((step for step in steps if not step['done']), None),
    })


def latest_payment_id(user):
    return Payment.objects.filter(lease__unit__property__owner=user).aggregate(m=Max('id'))['m'] or 0


@login_required
def live(request):
    """Background check from the browser: payments newer than `since`, and fresh dashboard parts.

    Polling every few seconds mustn't count as activity, or idle sessions would never expire;
    request.passive_session tells the session middleware not to extend the session.
    """
    request.passive_session = True
    user = request.user
    try:
        since = int(request.GET.get('since', '0'))
    except ValueError:
        since = 0
    new = list(
        Payment.objects.filter(lease__unit__property__owner=user, pk__gt=since)
        .select_related('lease__tenant', 'lease__unit').order_by('pk')[:20]
    )
    data = {
        'cursor': max([since, latest_payment_id(user)]),
        'payments': [{
            'id': p.pk,
            'amount': f'KES {p.amount:,.0f}',
            'tenant': p.lease.tenant.full_name,
            'unit': p.lease.unit.label,
            'method': p.get_method_display(),
            'url': reverse('rentals:lease_detail', args=[p.lease_id]),
        } for p in new],
    }
    if request.GET.get('fragments'):
        context = {**dashboard_numbers(user), 'request': request}
        data['fragments'] = {
            name: render_to_string(f'rentals/dashboard/_{name}.html', context, request=request)
            for name in ('kpis', 'arrears', 'recent')
        }
    return JsonResponse(data)


def statement(lease, limit=24):
    """Charges, credits and payments in date order with a running balance, newest first."""
    entries = []
    for c in lease.charges.all():
        entry = {'date': c.period if c.kind == RentCharge.Kind.RENT else c.due_date,
                 'kind': 'charge', 'description': c.label, 'charge_id': c.pk}
        if c.amount < 0:
            entry['payment'] = -c.amount  # credits reduce what's owed, like a payment
            entry['credit'] = True
        else:
            entry['charge'] = c.amount
        entries.append(entry)
    entries += [
        {
            'date': timezone.localdate(p.paid_at), 'kind': 'payment', 'payment': p.amount, 'payment_id': p.pk,
            'is_mpesa': p.method == Payment.Method.MPESA,
            'description': gettext('%(method)s payment') % {'method': p.get_method_display()}
                           + (f' · {p.reference}' if p.reference else ''),
        }
        for p in lease.payments.all()
    ]
    # On the same day, show charges before the payment that settles them.
    entries.sort(key=lambda e: (e['date'], e['kind'] == 'payment'))
    running = 0
    for e in entries:
        running += e.get('charge', 0) - e.get('payment', 0)
        e['balance'] = running
    newest_first = entries[::-1]
    return newest_first[:limit] if limit else newest_first


@login_required
def lease_detail(request, pk):
    lease = get_object_or_404(
        Lease.objects.select_related('tenant', 'unit__property'),
        pk=pk,
        unit__property__owner=request.user,
    )
    balance = lease.balance
    return render(request, 'rentals/lease_detail.html', {
        'lease': lease,
        'balance': balance,
        'statement': statement(lease),
        'deposit_held': lease.deposit_held,
        'voided': sorted(
            [*RentCharge.all_objects.filter(lease=lease, voided_at__isnull=False),
             *Payment.all_objects.filter(lease=lease, voided_at__isnull=False)],
            key=lambda r: r.voided_at, reverse=True,
        ),
        'history': lease.audit_entries.select_related('actor')[:15],
        'mpesa_transactions': lease.mpesa_transactions.all()[:10],
        'sms_messages': lease.sms_messages.all()[:10],
        'mpesa_account': MpesaAccount.objects.filter(owner=request.user).first(),
        'stk_form': StkPushForm(initial={'amount': max(balance, 1), 'phone': lease.tenant.phone}),
    })


@login_required
@require_POST
def send_reminder(request, pk):
    lease = get_object_or_404(Lease, pk=pk, unit__property__owner=request.user)
    msg = send_rent_reminder(lease)
    if msg.status == msg.Status.FAILED:
        messages.error(request, f'SMS failed: {msg.error}')
    else:
        messages.success(request, f'Reminder sent to {lease.tenant} ({msg.phone}).')
    next_url = request.POST.get('next')
    if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}, request.is_secure()):
        return redirect(next_url)
    return redirect('rentals:lease_detail', pk=lease.pk)


@tenant_page
def tenant_portal(request, token):
    """Public self-service page for one tenant, reached via the secret link sent by SMS."""
    tenant = get_object_or_404(Tenant, portal_token=token)
    leases = list(tenant.leases.filter(is_active=True).select_related('unit__property'))
    units = Unit.objects.filter(leases__in=leases)
    maintenance_form = TenantMaintenanceForm(units=units)

    if request.method == 'POST' and leases:
        if request.POST.get('action') == 'pay':
            lease = next((l for l in leases if str(l.pk) == request.POST.get('lease')), None)
            form = StkPushForm(request.POST)
            if lease is None or not form.is_valid():
                messages.error(request, gettext('Enter a valid amount.'))
            else:
                try:
                    # Always the tenant's own number, so the link can't be used to prompt strangers.
                    txn = mpesa.stk_push(lease, form.cleaned_data['amount'])
                except mpesa.MpesaError as exc:
                    # Problems with the landlord's M-Pesa setup aren't the tenant's to fix.
                    messages.error(request, str(exc) if exc.code in mpesa.TENANT_ERRORS else gettext(
                        "We couldn't send the M-Pesa prompt. Please try again later, or pay from the M-Pesa menu."))
                else:
                    messages.success(request, gettext(
                        'Check your phone (%(phone)s) and enter your M-Pesa PIN to pay KES %(amount)s.'
                    ) % {'phone': txn.phone, 'amount': txn.amount})
            return redirect('rentals:tenant_portal', token=token)

        if request.POST.get('action') == 'maintenance':
            maintenance_form = TenantMaintenanceForm(request.POST, units=units)
            if maintenance_form.is_valid():
                req = maintenance_form.save(commit=False)
                req.reported_by = tenant
                req.save()
                messages.success(request, gettext('Thanks, your request has been sent to your landlord.'))
                return redirect('rentals:tenant_portal', token=token)

    return render(request, 'rentals/tenant_portal.html', {
        'tenant': tenant,
        'lease_rows': [(lease, lease.balance) for lease in leases],
        'payments': Payment.objects.filter(lease__tenant=tenant).select_related('lease__unit')[:10],
        'requests': tenant.maintenance_requests.select_related('unit')[:10],
        'open_complaints': tenant.complaints.exclude(status='resolved').count(),
        'notices': current_notices(tenant),
        'maintenance_form': maintenance_form,
        'mpesa_account': MpesaAccount.objects.filter(owner=tenant.owner).first(),
    })
