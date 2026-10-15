"""The staff area (caretakers): prompt tenants to pay and confirm it arrived, record cash, log repairs.
And the landlord's pages for adding and managing staff (Settings → Staff)."""

import datetime
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST

from accounts.views import ChangePasswordView
from audit import utils as audit
from maintenance.models import MaintenanceRequest
from notifications.utils import send_cash_receipt
from payments import mpesa
from payments.forms import StkPushForm
from payments.models import MpesaAccount, MpesaTransaction, Payment

from .forms import CashForm, RepairForm, StaffForm
from .models import StaffMember

OPEN_REPAIRS = [MaintenanceRequest.Status.OPEN, MaintenanceRequest.Status.IN_PROGRESS]
RECENT_PROMPT = datetime.timedelta(minutes=10)


def staff_required(view):
    """Only staff accounts; landlords are sent to their own dashboard."""

    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if request.staff is None:
            return redirect('rentals:dashboard')
        return view(request, *args, **kwargs)

    return wrapper


def _lease(request, pk):
    return get_object_or_404(request.staff.leases().select_related('tenant', 'unit__property'), pk=pk)


def _record(request, action, summary, lease=None):
    audit.record(owner=request.staff.owner, actor=request.user, action=action, summary=summary, lease=lease)


# --- Staff area -------------------------------------------------------------------------------

@staff_required
def home(request):
    staff = request.staff
    show_all = request.GET.get('show') == 'all'
    q = request.GET.get('q', '').strip()
    leases = staff.leases().select_related('tenant', 'unit__property').order_by('unit__property__name', 'unit__label')
    if q:
        leases = leases.filter(Q(tenant__full_name__icontains=q) | Q(unit__label__icontains=q))
    last_paid = dict(Payment.objects.filter(lease__in=leases).values_list('lease').annotate(last=Max('paid_at')))
    prompts = {}
    for txn in MpesaTransaction.objects.filter(lease__in=leases, created_at__gte=timezone.now() - RECENT_PROMPT):
        prompts.setdefault(txn.lease_id, txn)  # newest first
    rows = []
    for lease in leases:
        balance = lease.balance
        if balance > 0 or show_all or q:
            rows.append({'lease': lease, 'balance': balance, 'last_paid': last_paid.get(lease.pk),
                         'prompt': prompts.get(lease.pk)})
    rows.sort(key=lambda r: -r['balance'])
    account = MpesaAccount.objects.filter(owner=staff.owner).first()
    return render(request, 'staff/home.html', {
        'rows': rows, 'show_all': show_all, 'q': q,
        'mpesa_ready': account is not None and account.is_complete(),
        'open_repairs': MaintenanceRequest.objects.filter(unit__in=staff.units(), status__in=OPEN_REPAIRS).count(),
    })


@staff_required
@require_POST
def send_prompt(request, pk):
    lease = _lease(request, pk)
    form = StkPushForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Enter a valid amount.')
        return redirect('staff:home')
    try:
        # Always the tenant's own number on file: staff can't prompt other phones.
        txn = mpesa.stk_push(lease, form.cleaned_data['amount'])
    except mpesa.MpesaError as exc:
        messages.error(request, str(exc))
        return redirect('staff:home')
    _record(request, 'staff.prompt_sent', f'Sent an M-Pesa prompt for KES {txn.amount:,.0f} to {lease.tenant}.', lease)
    return redirect('staff:prompt', pk=txn.pk)


def _staff_txn(request, pk):
    return get_object_or_404(MpesaTransaction.objects.select_related('lease__tenant', 'lease__unit', 'payment'),
                             pk=pk, lease__in=request.staff.leases())


def _prompt_state(txn):
    if txn.status == MpesaTransaction.Status.SUCCESS:
        return {'status': 'paid', 'title': f'Paid: KES {txn.amount:,.0f} received',
                'detail': f'M-Pesa receipt {txn.receipt_number}. {txn.lease.tenant} has been sent a receipt.'}
    if txn.status == MpesaTransaction.Status.FAILED:
        return {'status': 'failed', 'title': 'Not paid',
                'detail': txn.result_desc or 'The tenant cancelled, or the prompt timed out.'}
    return {'status': 'waiting', 'title': 'Waiting for the tenant to enter their M-Pesa PIN…',
            'detail': f'A prompt for KES {txn.amount:,.0f} was sent to {txn.phone}.'}


@staff_required
def prompt_status(request, pk):
    txn = _staff_txn(request, pk)
    state = _prompt_state(txn)
    if request.GET.get('format') == 'json':
        request.passive_session = True  # polling mustn't keep an idle session alive
        return JsonResponse(state)
    return render(request, 'staff/prompt.html', {'txn': txn, 'state': state})


@staff_required
@require_POST
def prompt_check(request, pk):
    """Ask Safaricom directly, for when the confirmation is slow to arrive."""
    txn = _staff_txn(request, pk)
    try:
        mpesa.query_stk_status(txn)
    except mpesa.MpesaError as exc:
        messages.error(request, f"Couldn't check with Safaricom just now: {exc}")
    return redirect('staff:prompt', pk=txn.pk)


@staff_required
def record_cash(request, pk):
    lease = _lease(request, pk)
    form = CashForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        notes = f'Cash collected by {request.staff.name} (staff)' + (f': {data["notes"]}' if data['notes'] else '')
        try:
            with transaction.atomic():
                payment = Payment.objects.create(lease=lease, amount=data['amount'], method=Payment.Method.CASH,
                                                 reference=data['reference'], paid_at=data['paid_at'],
                                                 notes=notes[:255])
        except IntegrityError:
            form.add_error('reference', 'A cash payment with this receipt number is already recorded.')
        else:
            _record(request, 'payment.recorded',
                    f'{request.staff.name} (staff) recorded a cash payment of KES {payment.amount:,.2f} '
                    f'from {lease.tenant}.', lease)
            send_cash_receipt(payment, request.staff.name)
            messages.success(request, f'Recorded KES {payment.amount:,.0f} cash from {lease.tenant}. '
                                      'They have been sent an SMS receipt.')
            return redirect('staff:home')
    return render(request, 'staff/form.html', {
        'form': form, 'title': f'Record cash from {lease.tenant}',
        'intro': f'{lease.unit} · owes KES {max(lease.balance, 0):,.0f}. The tenant gets an SMS receipt.',
        'submit': 'Record cash',
        'confirm': f'Record KES {{amount}} cash from {lease.tenant}? They will get an SMS receipt with your name.',
    })


@staff_required
def repairs(request):
    items = (MaintenanceRequest.objects.filter(unit__in=request.staff.units(), status__in=OPEN_REPAIRS)
             .select_related('unit__property', 'reported_by').order_by('-created_at'))
    return render(request, 'staff/repairs.html', {'repairs': items})


@staff_required
def repair_form(request, pk=None):
    instance = None
    if pk is not None:
        instance = get_object_or_404(MaintenanceRequest, pk=pk, unit__in=request.staff.units())
    form = RepairForm(request.POST or None, instance=instance, member=request.staff)
    if request.method == 'POST' and form.is_valid():
        repair = form.save()
        verb = 'updated' if instance else 'logged'
        _record(request, f'repair.{verb}', f'{request.staff.name} (staff) {verb} repair "{repair.title}" '
                                           f'({repair.unit}).')
        messages.success(request, f'Repair {verb}.')
        return redirect('staff:repairs')
    return render(request, 'staff/form.html', {
        'form': form, 'title': 'Update repair' if instance else 'Log a repair',
        'submit': 'Save' if instance else 'Log repair', 'cancel': reverse('staff:repairs'),
    })


@method_decorator(staff_required, name='dispatch')
class StaffPasswordView(ChangePasswordView):
    template_name = 'staff/form.html'
    success_url = reverse_lazy('staff:home')
    extra_context = {'title': 'Change your password', 'submit': 'Change password',
                     'intro': 'Anywhere else you are signed in will be signed out.'}


# --- Landlord: managing staff (Settings → Staff) --------------------------------------------

def landlord_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if request.staff is not None:  # staff can't manage staff
            return redirect('staff:home')
        return view(request, *args, **kwargs)

    return wrapper


@landlord_required
def staff_list(request):
    members = StaffMember.objects.filter(owner=request.user).select_related('user').prefetch_related('properties')
    return render(request, 'staff/manage_list.html', {'members': members})


@landlord_required
def staff_edit(request, pk=None):
    member = get_object_or_404(StaffMember, pk=pk, owner=request.user) if pk else None
    form = StaffForm(request.POST or None, owner=request.user, member=member)
    if request.method == 'POST' and form.is_valid():
        member = form.save()
        if pk:
            audit.record(owner=request.user, actor=request.user, action='staff.updated',
                         summary=f'Updated staff member {member.name}'
                                 + ('' if member.user.is_active else ' (can no longer log in)') + '.')
            messages.success(request, f'Saved {member.name}.')
        else:
            audit.record(owner=request.user, actor=request.user, action='staff.added',
                         summary=f'Added staff member {member.name} ({member.user.username}).')
            messages.success(request, f'Added {member.name}. They log in with the username "{member.user.username}" '
                                      'and the password you set.')
        return redirect('staff:manage')
    return render(request, 'form_page.html', {
        'form': form, 'page_title': f'Edit {member.name}' if member else 'Add staff',
        'intro': None if member else 'A caretaker or agent. They can ask tenants to pay by M-Pesa and see '
                                     'whether it arrived, record cash and log repairs. They never see your '
                                     'totals, reports or settings.',
        'submit_label': 'Save' if member else 'Add staff member', 'cancel_url': reverse('staff:manage'),
    })
