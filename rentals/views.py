import datetime
import math

from django.conf import settings
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
from audit import utils as audit
from maintenance.forms import TenantMaintenanceForm
from maintenance.models import MaintenanceRequest
from notifications.models import SmsMessage
from notifications.utils import plan_agreed_text, reminder_text, send_personal_sms, send_rent_reminder, send_sms
from payments import mpesa
from payments.forms import StkPushForm
from payments.models import C2BPayment, MpesaAccount, Payment

from . import utils as rent_changes
from .forms import BulkRentChangeForm, NoticeForm, PaymentPlanForm, RentChangeForm
from .utils import create_plan, on_track_plan, plan_progress, plan_schedule, refresh_plan, tenant_page
from .models import Lease, PaymentPlan, Property, RentCharge, Tenant, Unit


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
    rate = min(100, round(collected * 100 / billed)) if billed else None
    return {
        'today': today,
        # Rent-day ring: how much of this month's billing is in, and how many tenants are fully paid up.
        'tenants_paid': sum(1 for _, bal in lease_rows if bal <= 0),
        'tenants_owing': len(arrears),
        'ring_offset': 100 - (rate or 0),
        'open_requests': open_requests,
        'open_count': open_requests.count(),
        'urgent_count': open_requests.filter(
            priority__in=[MaintenanceRequest.Priority.HIGH, MaintenanceRequest.Priority.URGENT]
        ).count(),
        'unit_count': unit_count,
        'vacant_count': unit_count - len(lease_rows),
        'billed_this_month': billed,
        'collection_rate': rate,
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
        'moving_out': Lease.objects.filter(unit__property__owner=user, is_active=True, end_date__isnull=False)
        .select_related('tenant', 'unit__property').order_by('end_date')[:8],
        'setup_done': sum(step['done'] for step in steps),
        'setup_total': len(steps),
        'setup_next': next((step for step in steps if not step['done']), None),
        'plans': [{'plan': p, 'progress': refresh_plan(p)} for p in
                  PaymentPlan.objects.filter(lease__unit__property__owner=user, status=PaymentPlan.Status.ACTIVE)
                  .select_related('lease__tenant', 'lease__unit').order_by('started_at')[:8]],
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
        entry = {'date': c.due_date,  # rent: the rent day its month starts on
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
    plan = lease.payment_plans.filter(status=PaymentPlan.Status.ACTIVE).prefetch_related('instalments').first()
    return render(request, 'rentals/lease_detail.html', {
        'lease': lease,
        'balance': balance,
        'plan': plan,
        'plan_progress': refresh_plan(plan) if plan else None,
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


@login_required
def lease_notice(request, pk):
    """Record (or change, or cancel) a month-to-month tenant's notice to move out."""
    lease = get_object_or_404(Lease.objects.select_related('tenant', 'unit__property'), pk=pk,
                              unit__property__owner=request.user, is_active=True)
    if request.method == 'POST' and request.POST.get('action') == 'cancel':
        lease.end_date = lease.notice_given_on = lease.move_out_reminded_on = None
        lease.notice_note = ''
        lease.save(update_fields=['end_date', 'notice_given_on', 'notice_note', 'move_out_reminded_on'])
        audit.record(owner=request.user, actor=request.user, action='lease.notice_cancelled', lease=lease,
                     summary=f'Cancelled the move-out notice for {lease.tenant}: they are staying.')
        messages.success(request, f'Notice cancelled. {lease.tenant} stays on month to month.')
        return redirect('rentals:lease_detail', pk=lease.pk)

    form = NoticeForm(request.POST or None, lease=lease)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        changed = lease.end_date is not None
        lease.end_date, lease.notice_given_on, lease.notice_note = data['move_out'], data['given_on'], data['note']
        lease.move_out_reminded_on = None
        lease.save(update_fields=['end_date', 'notice_given_on', 'notice_note', 'move_out_reminded_on'])
        audit.record(owner=request.user, actor=request.user, action='lease.notice_given', lease=lease,
                     summary=f'{"Changed" if changed else "Recorded"} move-out notice: {lease.tenant} moves out of '
                             f'{lease.unit} on {lease.end_date:%d %b %Y}.')
        messages.success(request, f'{lease.tenant} moves out on {lease.end_date:%d %b %Y}. Rent is billed up to '
                                  f'{lease.end_date:%B}; you\'ll get a reminder a few days before.')
        return redirect('rentals:lease_detail', pk=lease.pk)
    return render(request, 'rentals/lease_notice.html', {'form': form, 'lease': lease})


@login_required
def lease_rent_change(request, pk):
    """Plan (or cancel) a rent change for one tenant."""
    lease = get_object_or_404(Lease.objects.select_related('tenant', 'unit__property'), pk=pk,
                              unit__property__owner=request.user, is_active=True)
    pending = lease.pending_rent_change()
    if request.method == 'POST' and request.POST.get('action') == 'cancel' and pending:
        rent_changes.cancel(pending, actor=request.user)
        messages.success(request, 'Planned rent change cancelled. Their rent stays the same.')
        return redirect('rentals:lease_detail', pk=lease.pk)
    form = RentChangeForm(request.POST or None, lease=lease)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        (change,), sms = rent_changes.plan([(lease, data['new_rent'])], data['effective_from'],
                                           notify=data['notify'], actor=request.user)
        told = ' They have been sent an SMS.' if sms and sms[0].status != SmsMessage.Status.FAILED else ''
        messages.success(request, f'Rent changes to KES {change.new_rent:,.0f} from {change.starts_on:%d %b %Y}.{told}')
        return redirect('rentals:lease_detail', pk=lease.pk)
    return render(request, 'rentals/rent_change.html', {'form': form, 'lease': lease, 'pending': pending})


@login_required
def property_rent_change(request, pk):
    """Change rent for several tenants in a property: choose, preview old → new, then confirm."""
    prop = get_object_or_404(Property, pk=pk, owner=request.user)
    form = BulkRentChangeForm(request.POST or None, property=prop)
    preview = None
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        rows = [(lease, rent_changes.new_rent_for(lease.monthly_rent, data['mode'], data['value']))
                for lease in data['leases']]
        rows = [(lease, rent) for lease, rent in rows if rent != lease.monthly_rent]
        if request.POST.get('confirm') and rows:
            changes, sms = rent_changes.plan(rows, data['effective_from'], notify=data['notify'], actor=request.user)
            failed = sum(m.status == SmsMessage.Status.FAILED for m in sms)
            messages.success(request, f'Planned new rent for {len(changes)} tenant{"s" if len(changes) != 1 else ""}.'
                             + (f' SMS sent to {len(sms) - failed}.' if sms else '')
                             + (f' {failed} SMS failed: see the SMS log.' if failed else ''))
            return redirect('rentals:property_detail', pk=prop.pk)
        preview = [{'lease': lease, 'new': rent, 'starts': lease.first_rent_day_from(data['effective_from'])}
                   for lease, rent in rows]
    return render(request, 'rentals/rent_change_bulk.html', {'form': form, 'property': prop, 'preview': preview})


@login_required
def plan_create(request, pk):
    """Agree a payment plan for a tenant's arrears: fill in, preview the schedule, then save."""
    lease = get_object_or_404(Lease.objects.select_related('tenant', 'unit__property'), pk=pk,
                              unit__property__owner=request.user, is_active=True)
    if lease.payment_plans.filter(status=PaymentPlan.Status.ACTIVE).exists():
        messages.info(request, 'This tenant already has a payment plan. Cancel it first to agree a new one.')
        return redirect('rentals:lease_detail', pk=lease.pk)
    form = PaymentPlanForm(request.POST or None, lease=lease)
    schedule = None
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        if 'save' in request.POST:
            plan = create_plan(lease, total=data['total'], instalment=data['instalment'], frequency=data['frequency'],
                               first_due=data['first_due'], note=data['note'], actor=request.user)
            sent = ''
            if data['notify']:
                msg = send_sms(lease.tenant.phone, plan_agreed_text(plan), lease, SmsMessage.Kind.OTHER)
                sent = ' They have been sent the plan by SMS.' if msg.status != SmsMessage.Status.FAILED else ''
            messages.success(request, f'Payment plan agreed: {plan.instalments.count()} payments.{sent}')
            return redirect('rentals:lease_detail', pk=lease.pk)
        schedule = plan_schedule(data['total'], data['instalment'], data['frequency'], data['first_due'])
    return render(request, 'rentals/plan_form.html', {'form': form, 'lease': lease, 'schedule': schedule})


@login_required
@require_POST
def plan_cancel(request, pk):
    plan = get_object_or_404(PaymentPlan.objects.select_related('lease__tenant'), pk=pk,
                             lease__unit__property__owner=request.user, status=PaymentPlan.Status.ACTIVE)
    plan.status, plan.ended_at = PaymentPlan.Status.CANCELLED, timezone.now()
    plan.save(update_fields=['status', 'ended_at'])
    audit.record(owner=request.user, actor=request.user, action='lease.plan_cancelled', lease=plan.lease,
                 summary=f'Cancelled the payment plan for {plan.lease.tenant}.')
    messages.success(request, 'Payment plan cancelled. Normal balance reminders apply again.')
    return redirect('rentals:lease_detail', pk=plan.lease_id)


@login_required
def plan_list(request):
    status = request.GET.get('status', 'active')
    plans = (PaymentPlan.objects.filter(lease__unit__property__owner=request.user)
             .select_related('lease__tenant', 'lease__unit__property').prefetch_related('instalments'))
    if status in PaymentPlan.Status.values:
        plans = plans.filter(status=status)
    rows = [{'plan': p, 'progress': refresh_plan(p) if p.status == PaymentPlan.Status.ACTIVE else plan_progress(p)}
            for p in plans]
    return render(request, 'rentals/plan_list.html', {'rows': rows, 'status': status,
                                                       'behind': sum(1 for r in rows if not r['progress']['on_track'])})


RECENT_REMINDER = datetime.timedelta(hours=24)


def _sms_parts(text):
    gsm = all(ord(c) < 128 for c in text)  # emoji/accents switch phones to 70-char parts
    single, multi = (160, 153) if gsm else (70, 67)
    return 1 if len(text) <= single else math.ceil(len(text) / multi)


@login_required
def remind_all(request):
    """Preview, then send, a rent reminder to every tenant who owes (one tap from the dashboard)."""
    leases = (Lease.objects.filter(unit__property__owner=request.user, is_active=True)
              .select_related('tenant', 'unit__property').order_by('unit__property__name', 'unit__label'))
    owing = [(lease, balance) for lease in leases if (balance := lease.balance) > 0]
    last_reminded = dict(
        SmsMessage.objects.filter(kind=SmsMessage.Kind.REMINDER, lease__in=[lease for lease, _ in owing],
                                  created_at__gte=timezone.now() - RECENT_REMINDER)
        .exclude(status=SmsMessage.Status.FAILED)
        .values_list('lease_id').annotate(last=Max('created_at'))
    )

    if request.method == 'POST':
        chosen = set(request.POST.getlist('lease'))
        targets = [(lease, balance) for lease, balance in owing if str(lease.pk) in chosen]
        if not targets:
            messages.error(request, 'Choose at least one tenant to remind.')
            return redirect('rentals:remind_all')
        sent = send_personal_sms(
            [(lease, lease.tenant.phone, reminder_text(lease, balance)) for lease, balance in targets],
            kind=SmsMessage.Kind.REMINDER,
        )
        failed = sum(m.status == SmsMessage.Status.FAILED for m in sent)
        ok = len(sent) - failed
        audit.record(owner=request.user, actor=request.user, action='reminders.sent',
                     summary=f'Sent rent reminders to {ok} tenant(s)' + (f', {failed} failed.' if failed else '.'))
        if failed:
            messages.warning(request, f'Sent {ok} reminder{"s" if ok != 1 else ""}; {failed} failed. '
                                      'See the SMS log for why.')
        else:
            messages.success(request, f'Sent {ok} reminder{"s" if ok != 1 else ""}.')
        return redirect('rentals:dashboard')

    rows = []
    for lease, balance in owing:
        text = reminder_text(lease, balance)
        rows.append({'lease': lease, 'balance': balance, 'text': text, 'parts': _sms_parts(text),
                     'reminded_at': last_reminded.get(lease.pk), 'on_plan': on_track_plan(lease)})
    selected = [r for r in rows if not r['reminded_at'] and not r['on_plan']]
    return render(request, 'rentals/remind_all.html', {
        'rows': rows,
        'selected_count': len(selected),
        'total_owed': sum(r['balance'] for r in rows),
        'cost_each': settings.SMS_COST_PER_PART,
        'estimated_cost': round(sum(r['parts'] for r in selected) * settings.SMS_COST_PER_PART, 2),
    })


def _portal_plan(lease):
    plan = lease.payment_plans.filter(status=PaymentPlan.Status.ACTIVE).first()
    if plan is None:
        return None
    progress = refresh_plan(plan)
    return None if progress['done'] else {'plan': plan, 'progress': progress}


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
        'lease_rows': [(lease, lease.balance, _portal_plan(lease)) for lease in leases],
        'payments': Payment.objects.filter(lease__tenant=tenant).select_related('lease__unit')[:10],
        'requests': tenant.maintenance_requests.select_related('unit')[:10],
        'open_complaints': tenant.complaints.exclude(status='resolved').count(),
        'notices': current_notices(tenant),
        'maintenance_form': maintenance_form,
        'mpesa_account': MpesaAccount.objects.filter(owner=tenant.owner).first(),
    })
