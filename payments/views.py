import datetime
import json
import logging
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView

from accounts import utils as ratelimit
from accounts.utils import is_verified
from audit import utils as audit
from rentals.crud_views import FormPageMixin
from rentals.utils import OwnedMixin
from rentals.models import Lease

from . import mpesa
from .forms import (
    AssignC2BForm, ManualPaymentForm, MovePaymentForm, MpesaAccountForm, StatementUploadForm, StkPushForm, VoidForm,
)
from .models import C2BPayment, MpesaAccount, MpesaTransaction, Payment
from .utils import STATEMENT_MAX_BYTES, StatementError, apply_statement, check_statement, read_statement

logger = logging.getLogger(__name__)

ACCEPTED = {'ResultCode': 0, 'ResultDesc': 'Accepted'}


def _json_body(request):
    try:
        return json.loads(request.body)
    except ValueError:
        return None


@csrf_exempt
@require_POST
def stk_callback(request, token):
    account = get_object_or_404(MpesaAccount, callback_token=token)
    ip = ratelimit.client_ip(request)
    if not mpesa.callback_is_trusted(account, ip):
        # Not from Safaricom: ignore it. A genuine payment is still picked up, because the
        # scheduled status check asks Safaricom directly about any prompt left pending.
        logger.warning('Ignored STK callback for account %s from untrusted address %s', account.pk, ip)
        return JsonResponse(ACCEPTED)
    try:
        mpesa.handle_stk_callback(account, _json_body(request))
    except (KeyError, TypeError, ValueError):
        logger.exception('Malformed STK callback: %r', request.body[:1000])
    # Always acknowledge so Safaricom does not keep retrying.
    return JsonResponse(ACCEPTED)


@csrf_exempt
@require_POST
def c2b_validation(request, token):
    """Only called if Safaricom has enabled external validation on the shortcode.

    Rejects payments we cannot attribute to a lease, so the tenant gets an error
    on their phone instead of the money landing unassigned.
    """
    account = get_object_or_404(MpesaAccount, callback_token=token)
    body = _json_body(request) or {}
    if mpesa.match_lease(account, body.get('BillRefNumber'), body.get('MSISDN')) is None:
        return JsonResponse({'ResultCode': 'C2B00012', 'ResultDesc': 'Rejected'})
    return JsonResponse(ACCEPTED)


@csrf_exempt
@require_POST
def c2b_confirmation(request, token):
    account = get_object_or_404(MpesaAccount, callback_token=token)
    ip = ratelimit.client_ip(request)
    try:
        mpesa.handle_c2b_confirmation(
            account, _json_body(request), trusted=mpesa.callback_is_trusted(account, ip), source_ip=ip,
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        logger.exception('Malformed C2B confirmation: %r', request.body[:1000])
    return JsonResponse(ACCEPTED)


@login_required
@require_POST
def request_payment(request, lease_id):
    lease = get_object_or_404(
        Lease.objects.select_related('tenant', 'unit'),
        pk=lease_id,
        unit__property__owner=request.user,
    )
    form = StkPushForm(request.POST)
    if form.is_valid():
        try:
            txn = mpesa.stk_push(lease, form.cleaned_data['amount'], form.cleaned_data['phone'] or None)
        except mpesa.MpesaError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f'Payment prompt for KES {txn.amount} sent to {txn.phone}. '
                'It will appear here once the tenant confirms.',
            )
    else:
        messages.error(request, 'Invalid amount or phone number.')
    return redirect('rentals:lease_detail', pk=lease.pk)


@login_required
def mpesa_settings(request):
    account = MpesaAccount.objects.filter(owner=request.user).first()
    form = MpesaAccountForm(instance=account)

    verified = is_verified(request.user)
    if request.method == 'POST' and not verified:
        messages.error(request, 'Confirm your email address before connecting M-Pesa.')
        return redirect('payments:mpesa_settings')
    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        if action == 'save':
            form = MpesaAccountForm(request.POST, instance=account)
            if form.is_valid():
                account = form.save(commit=False)
                account.owner = request.user
                account.save()
                audit.record(
                    owner=request.user, actor=request.user, action='mpesa.settings_changed',
                    summary=f'Changed M-Pesa settings ({account.get_payment_type_display()} {account.pay_number}).',
                    changes=audit.form_changes(form),
                )
                if account.is_complete():
                    messages.success(request, 'M-Pesa settings saved. Next, test the connection.')
                else:
                    messages.success(request, f'Saved. Tenants will be told to pay to your '
                                              f'{account.get_payment_type_display()} {account.pay_number}. '
                                              'Upload your M-Pesa statement to record their payments.')
                return redirect('payments:mpesa_settings')
        elif account is None:
            messages.error(request, 'Save your M-Pesa details first.')
            return redirect('payments:mpesa_settings')
        else:
            try:
                if action == 'verify':
                    mpesa.verify_credentials(account)
                    messages.success(request, 'Connected. Safaricom accepted your credentials.')
                elif action == 'register':
                    mpesa.register_c2b_urls(account)
                    messages.success(
                        request,
                        f'Registered. Payments to {account.get_payment_type_display()} '
                        f'{account.pay_number} will now be recorded automatically.',
                    )
            except mpesa.MpesaError as exc:
                messages.error(request, str(exc))
            return redirect('payments:mpesa_settings')

    hooks = {}
    if account and account.is_complete():
        try:
            hooks = {
                label: mpesa.hook_url(account, name)
                for label, name in [('STK callback', 'stk_callback'), ('C2B confirmation', 'c2b_confirmation'),
                                    ('C2B validation', 'c2b_validation')]
            }
        except mpesa.MpesaError:
            pass
    return render(request, 'payments/mpesa_settings.html', {
        'form': form,
        'account': account,
        'hooks': hooks,
        'verified': verified,
    })


class PaymentList(OwnedMixin, ListView):
    model = Payment
    owner_path = 'lease__unit__property__owner'
    template_name = 'payments/payment_list.html'
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related('lease__tenant', 'lease__unit__property')
        method = self.request.GET.get('method')
        if method in Payment.Method.values:
            qs = qs.filter(method=method)
        month = self.request.GET.get('month')  # YYYY-MM
        if month:
            try:
                start = datetime.datetime.strptime(month, '%Y-%m').date()
            except ValueError:
                pass
            else:
                qs = qs.filter(paid_at__date__year=start.year, paid_at__date__month=start.month)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            total=self.get_queryset().aggregate(total=Sum('amount'))['total'] or 0,
            methods=Payment.Method.choices,
            method=self.request.GET.get('method', ''),
            month=self.request.GET.get('month', ''),
            unmatched_count=C2BPayment.objects.filter(account__owner=self.request.user, payment__isnull=True).count(),
        )
        return ctx


class PaymentCreate(OwnedMixin, FormPageMixin, CreateView):
    model = Payment
    audit_action = 'payment.recorded'
    audit_summary = 'Recorded {obj.method} payment of KES {obj.amount:,.2f} from {obj.lease.tenant}.'
    form_class = ManualPaymentForm
    owner_path = 'lease__unit__property__owner'
    page_title = 'Record a payment'
    submit_label = 'Record payment'
    cancel_url = reverse_lazy('payments:payment_list')
    success_message = 'Recorded KES {obj.amount:,.0f} from {obj.lease.tenant}.'
    intro = 'For cash or bank payments. M-Pesa payments are recorded automatically.'

    def get_initial(self):
        initial = super().get_initial()
        if self.request.GET.get('lease'):
            initial['lease'] = self.request.GET['lease']
        return initial

    def form_valid(self, form):
        form.instance.recorded_by = self.request.user
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        return super().get_context_data(intro=self.intro, **kwargs)

    def get_success_url(self):
        return reverse('rentals:lease_detail', args=[self.object.lease_id])


class UnmatchedList(OwnedMixin, ListView):
    """M-Pesa payments into the landlord's Paybill/Till that couldn't be tied to a tenant."""

    model = C2BPayment
    owner_path = 'account__owner'
    template_name = 'payments/unmatched_list.html'

    def get_queryset(self):
        return super().get_queryset().filter(payment__isnull=True)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['rows'] = [
            (c2b, AssignC2BForm(owner=self.request.user, prefix=f'c{c2b.pk}')) for c2b in ctx['object_list']
        ]
        ctx['held_count'] = sum(c2b.held_for_review for c2b in ctx['object_list'])
        return ctx


@login_required
@require_POST
def assign_unmatched(request, pk):
    c2b = get_object_or_404(C2BPayment, pk=pk, account__owner=request.user, payment__isnull=True)
    form = AssignC2BForm(request.POST, owner=request.user, prefix=f'c{c2b.pk}')
    if form.is_valid():
        with transaction.atomic():
            c2b.lease = form.cleaned_data['lease']
            c2b.held_for_review = False
            mpesa.apply_c2b_payment(c2b)
            c2b.save()
        audit.record(
            owner=request.user, actor=request.user, lease=c2b.lease, action='payment.assigned',
            summary=f'Assigned unmatched M-Pesa payment {c2b.trans_id} (KES {c2b.amount:,.2f}) to {c2b.lease.tenant}.',
        )
        messages.success(request, f'Assigned KES {c2b.amount:,.0f} ({c2b.trans_id}) to {c2b.lease.tenant}.')
    else:
        messages.error(request, 'Choose a tenant to assign this payment to.')
    return redirect('payments:unmatched_list')


def _owned_payment(request, pk):
    return get_object_or_404(
        Payment.objects.select_related('lease__tenant', 'lease__unit__property'),
        pk=pk, lease__unit__property__owner=request.user,
    )


@login_required
def payment_void(request, pk):
    """Void a cash/bank payment entered by mistake. It stays in the history, marked voided."""
    payment = _owned_payment(request, pk)
    back = reverse('rentals:lease_detail', args=[payment.lease_id])
    if payment.method == Payment.Method.MPESA:
        messages.error(request, "M-Pesa payments can't be voided because the money really arrived. "
                                'Move it to the right tenant instead.')
        return redirect(back)
    form = VoidForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        payment.void(request.user, form.cleaned_data['reason'])
        audit.record(
            owner=request.user, actor=request.user, lease=payment.lease, action='payment.voided',
            summary=f'Voided {payment.get_method_display()} payment of KES {payment.amount:,.2f} '
                    f'from {payment.lease.tenant}: {form.cleaned_data["reason"]}',
        )
        messages.success(request, f'Voided the KES {payment.amount:,.0f} payment. Their balance has been updated.')
        return redirect(back)
    return render(request, 'form_page.html', {
        'form': form, 'page_title': 'Void payment', 'submit_label': 'Void payment', 'cancel_url': back,
        'confirm': f'KES {payment.amount:,.0f} will be taken off what {payment.lease.tenant} has paid, so their balance goes up.',
        'intro': f'{payment.get_method_display()} payment of KES {payment.amount:,.2f} from {payment.lease.tenant} '
                 f'on {timezone.localtime(payment.paid_at):%d %b %Y}. It will no longer count towards their balance.',
    })


@login_required
def payment_move(request, pk):
    """Move a payment (usually M-Pesa) to the lease it was really meant for."""
    payment = _owned_payment(request, pk)
    old_lease = payment.lease
    back = reverse('rentals:lease_detail', args=[old_lease.pk])
    form = MovePaymentForm(request.POST or None, owner=request.user, exclude_lease=old_lease)
    if request.method == 'POST' and form.is_valid():
        new_lease = form.cleaned_data['lease']
        with transaction.atomic():
            payment.lease = new_lease
            payment.save(update_fields=['lease'])
            C2BPayment.objects.filter(payment=payment).update(lease=new_lease)
        summary = (f'Moved payment {payment.reference or payment.pk} of KES {payment.amount:,.2f} '
                   f'from {old_lease.tenant} to {new_lease.tenant}: {form.cleaned_data["reason"]}')
        for lease in (old_lease, new_lease):
            audit.record(owner=request.user, actor=request.user, lease=lease, action='payment.moved', summary=summary)
        messages.success(request, f'Moved KES {payment.amount:,.0f} to {new_lease.tenant}.')
        return redirect('rentals:lease_detail', pk=new_lease.pk)
    return render(request, 'form_page.html', {
        'form': form, 'page_title': 'Move payment', 'submit_label': 'Move payment', 'cancel_url': back,
        'confirm': f'KES {payment.amount:,.0f} will move to the tenant you chose.',
        'intro': f'{payment.get_method_display()} payment {payment.reference} of KES {payment.amount:,.2f}, currently '
                 f'credited to {old_lease.tenant}. Choose who actually paid.',
    })


@login_required
@require_POST
def stk_check(request, pk):
    """Manually ask Safaricom about one pending STK prompt."""
    txn = get_object_or_404(MpesaTransaction, pk=pk, lease__unit__property__owner=request.user)
    try:
        txn = mpesa.query_stk_status(txn)
    except mpesa.MpesaError as exc:
        messages.error(request, f"Couldn't check with Safaricom: {exc}")
    else:
        if txn.status == MpesaTransaction.Status.SUCCESS:
            messages.success(request, f'Paid. KES {txn.amount:,.0f} has been credited to {txn.lease.tenant}.')
        elif txn.status == MpesaTransaction.Status.FAILED:
            messages.info(request, f'Not paid: {txn.result_desc or "the prompt failed"}.')
        else:
            messages.info(request, 'Still waiting for the tenant to respond on their phone.')
    return redirect('rentals:lease_detail', pk=txn.lease_id)


STATEMENT_SESSION_KEY = 'statement_upload'


@login_required
def statement_upload(request):
    """Record payments from an M-Pesa statement: upload it, check the preview, then confirm."""
    account = MpesaAccount.objects.filter(owner=request.user).first()
    if account is None or not account.has_pay_number:
        messages.info(request, 'First add the Paybill or Till number your tenants pay to.')
        return redirect('payments:mpesa_settings')
    form = StatementUploadForm(request.POST or None, request.FILES or None)
    ctx = {'form': form, 'account': account}
    action = request.POST.get('action')

    if request.method == 'POST' and action == 'confirm':
        data = request.session.get(STATEMENT_SESSION_KEY)
        if not data:
            messages.error(request, 'That preview has expired. Please upload the statement again.')
            return redirect('payments:statement_upload')
        checked = check_statement(data['rows'], account)  # again: payments may have arrived meanwhile
        matched, unmatched = apply_statement(checked, account, request.user, send_receipts=data['send_receipts'])
        del request.session[STATEMENT_SESSION_KEY]
        if unmatched:
            messages.success(request, f'Recorded {matched} payment{"s" if matched != 1 else ""}. '
                                      f'{unmatched} couldn\'t be matched to a tenant: assign them below.')
            return redirect('payments:unmatched_list')
        messages.success(request, f'Recorded {matched} payment{"s" if matched != 1 else ""} from your statement.')
        return redirect('payments:payment_list')

    if request.method == 'POST' and form.is_valid():
        upload = form.cleaned_data['file']
        if upload.size > STATEMENT_MAX_BYTES:
            ctx['error'] = 'That file is over 5 MB. Download a shorter date range.'
        else:
            try:
                rows = read_statement(upload)
            except StatementError as exc:
                ctx['error'] = str(exc)
            else:
                checked = check_statement(rows, account)
                request.session[STATEMENT_SESSION_KEY] = {
                    'rows': rows, 'send_receipts': form.cleaned_data['send_receipts']}
                counts = {state: sum(1 for r in checked if r['state'] == state)
                          for state in ('match', 'unmatched', 'recorded', 'error')}
                ctx.update(checked=checked, counts=counts,
                           new_total=sum(Decimal(r['amount']) for r in checked if r['state'] in ('match', 'unmatched')))
    return render(request, 'payments/statement_upload.html', ctx)

