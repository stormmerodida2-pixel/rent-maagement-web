"""Billing: the landlord's Billing page (plan, bills, paying), the M-Pesa callback for bill prompts, and the
administrator's Billing overview (confirming Till payments, extending trials)."""

import datetime
import hmac
import json
import logging

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accounts.utils import client_ip
from notifications.models import LandlordPreferences

from .forms import BillingSettingsForm
from .models import BillingSettings, Invoice
from .utils import (
    ClaimError, account_for, claim_till_payment, conf, enabled, landlords, lock_date, mark_paid, monthly_amount,
    occupied_units, open_invoice, platform_account, send_prompt, status,
)
from .utils import handle_prompt_callback

logger = logging.getLogger(__name__)

ACCEPTED = {'ResultCode': 0, 'ResultDesc': 'Accepted'}


def _landlord_account(request):
    if not enabled() or request.user.is_superuser:
        raise Http404
    return account_for(request.user)


@login_required
def billing(request):
    account = _landlord_account(request)
    units = occupied_units(request.user)
    invoice = open_invoice(account)
    return render(request, 'billing/billing.html', {
        'account': account, 'state': status(account), 'invoice': invoice, 'units': units,
        'estimate': monthly_amount(units), 'free_units': conf('FREE_UNITS'), 'price': conf('PRICE_PER_UNIT'),
        'grace_days': conf('GRACE_DAYS'), 'lock_date': lock_date(account), 'till': conf('TILL'),
        'can_prompt': platform_account() is not None,
        'phone': LandlordPreferences.for_user(request.user).phone,
        'history': account.invoices.exclude(status=Invoice.Status.VOID).exclude(method=Invoice.Method.FREE)[:24],
    })


@login_required
@require_POST
def pay(request):
    from payments import mpesa

    account = _landlord_account(request)
    invoice = open_invoice(account)
    if invoice is None:
        return redirect('billing:billing')
    try:
        send_prompt(invoice, request.POST.get('phone', ''))
    except mpesa.MpesaError as exc:
        messages.error(request, f"We couldn't send the M-Pesa prompt: {exc}")
    else:
        messages.success(request, f'Check your phone and enter your M-Pesa PIN to pay KES {invoice.amount:,}. '
                                  'This page updates once it goes through.')
    return redirect('billing:billing')


@login_required
@require_POST
def claim(request):
    account = _landlord_account(request)
    invoice = open_invoice(account)
    if invoice is None:
        return redirect('billing:billing')
    try:
        claim_till_payment(invoice, request.POST.get('code', ''))
    except ClaimError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Thanks. We'll check the payment and send a receipt. Everything stays switched "
                                  'on meanwhile.')
    return redirect('billing:billing')


@login_required
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related('account__owner'), pk=pk)
    if invoice.account.owner != request.user and not request.user.is_superuser:
        raise Http404
    return render(request, 'billing/invoice.html', {'invoice': invoice, 'till': conf('TILL')})


@csrf_exempt
@require_POST
def stk_callback(request, token):
    from payments import mpesa

    account = platform_account()
    if account is None or not hmac.compare_digest(token, conf('CALLBACK_TOKEN')):
        raise Http404
    if not mpesa.callback_is_trusted(account, client_ip(request)):
        logger.warning('Bill prompt callback from untrusted address %s', client_ip(request))
        return HttpResponseForbidden()
    try:
        handle_prompt_callback(json.loads(request.body))
    except (ValueError, KeyError, TypeError):
        logger.exception('Bad bill prompt callback')
    return JsonResponse(ACCEPTED)


# --- Administrator: Billing overview ----------------------------------------------------------------------

superuser_required = user_passes_test(lambda u: u.is_superuser)


@staff_member_required
@superuser_required
def overview(request, form=None):
    today, on = timezone.localdate(), enabled()
    rows = []
    for owner in landlords().order_by('username'):
        # While billing is off, nobody's trial starts just because this page was opened.
        account = account_for(owner, today) if on else None
        rows.append({'owner': owner, 'account': account, 'units': occupied_units(owner),
                     'state': status(account, today) if on else 'off',
                     'invoice': open_invoice(account) if on else None})
    order = {'checking': 0, 'locked': 1, 'overdue': 2, 'active': 3, 'trial': 4, 'off': 5}
    rows.sort(key=lambda r: order[r['state']])
    billing_settings = BillingSettings.load()
    paid = Invoice.objects.filter(status=Invoice.Status.PAID, paid_at__date__gte=today.replace(day=1)) \
        .exclude(method=Invoice.Method.FREE)
    return render(request, 'billing/overview.html', {
        'rows': rows, 'enabled': on, 'till': conf('TILL'), 'billing_settings': billing_settings,
        'form': form or BillingSettingsForm(instance=billing_settings),
        'prompts_ready': billing_settings.prompts_ready(),
        'paid_this_month': sum(i.amount for i in paid),
        'monthly_estimate': sum(monthly_amount(r['units']) for r in rows if r['state'] not in ('trial', 'off')),
        'counts': {s: sum(1 for r in rows if r['state'] == s) for s in order},
    })


@staff_member_required
@superuser_required
@require_POST
def overview_mark_paid(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk, status=Invoice.Status.OPEN)
    code = request.POST.get('code', '').strip().upper()
    if code and Invoice.objects.filter(reference=code).exclude(pk=invoice.pk).exists():
        messages.error(request, f'{code} is already recorded against another bill.')
        return redirect('billing:overview')
    mark_paid(invoice, Invoice.Method.TILL if (code or invoice.claimed_reference) else Invoice.Method.MANUAL,
              code, actor=request.user)
    messages.success(request, f'{invoice.number} marked paid. {invoice.account.owner} has been sent a receipt.')
    return redirect('billing:overview')


@staff_member_required
@superuser_required
@require_POST
def overview_reject_claim(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk, status=Invoice.Status.OPEN)
    invoice.claimed_reference, invoice.claimed_at = '', None
    invoice.save(update_fields=['claimed_reference', 'claimed_at'])
    messages.success(request, f'Cleared the M-Pesa code on {invoice.number}. It counts as unpaid again.')
    return redirect('billing:overview')


@staff_member_required
@superuser_required
@require_POST
def overview_extend(request, pk):
    account = account_for(get_object_or_404(landlords(), pk=pk))
    try:
        days = max(1, min(int(request.POST.get('days', '30')), 365))
    except ValueError:
        days = 30
    account.paid_until = max(account.paid_until, timezone.localdate()) + datetime.timedelta(days=days)
    account.save(update_fields=['paid_until'])
    invoice = open_invoice(account)
    if invoice:  # its period is now covered for free
        invoice.status = Invoice.Status.VOID
        invoice.save(update_fields=['status'])
    messages.success(request, f'{account.owner} has {days} more free days, until {account.paid_until:%d %b %Y}.')
    return redirect('billing:overview')


@staff_member_required
@superuser_required
@require_POST
def overview_settings(request):
    from audit import utils as audit

    form = BillingSettingsForm(request.POST, instance=BillingSettings.load())
    if not form.is_valid():
        messages.error(request, 'Check the billing settings below.')
        return overview(request, form=form)
    form.save()
    audit.record(owner=request.user, actor=request.user, action='billing.settings_changed',
                 summary='Changed billing settings: ' + ', '.join(form.changed_data or ['nothing']) + '.')
    messages.success(request, 'Billing settings saved.')
    return redirect('billing:overview')


@staff_member_required
@superuser_required
@require_POST
def overview_toggle(request):
    from audit import utils as audit

    row = BillingSettings.load()
    if not row.enabled and not row.till:
        messages.error(request, 'Add your M-Pesa Till number first, so landlords know where to pay.')
        return redirect('billing:overview')
    row.enabled = not row.enabled
    row.save()
    today = timezone.localdate()
    if row.enabled:
        for owner in landlords():
            account = account_for(owner, today)  # new landlords: trial starts today
            if account.paid_until < today:  # switched on again after a pause: start afresh, no backdated bills
                account.invoices.filter(status=Invoice.Status.OPEN).update(status=Invoice.Status.VOID)
                account.paid_until = today
                account.save(update_fields=['paid_until'])
        messages.success(request, f'Billing is on. Every landlord has a {row.trial_days}-day free trial from today; '
                                  'first bills go out 3 days before it ends.')
    else:
        messages.success(request, 'Billing is off. Nobody is billed or read-only until you switch it on again.')
    audit.record(owner=request.user, actor=request.user, action='billing.switched',
                 summary=f'Switched billing {"on" if row.enabled else "off"}.')
    return redirect('billing:overview')

