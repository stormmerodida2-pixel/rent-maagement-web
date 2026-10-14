import datetime
import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView

from accounts.verification import is_verified
from rentals.crud_views import FormPageMixin
from rentals.mixins import OwnedMixin
from rentals.models import Lease

from . import mpesa
from .forms import AssignC2BForm, ManualPaymentForm, MpesaAccountForm, StkPushForm
from .models import C2BPayment, MpesaAccount, Payment

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
    try:
        mpesa.handle_c2b_confirmation(account, _json_body(request))
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
                messages.success(request, 'M-Pesa settings saved. Next, test the connection.')
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
    if account:
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
        return ctx


@login_required
@require_POST
def assign_unmatched(request, pk):
    c2b = get_object_or_404(C2BPayment, pk=pk, account__owner=request.user, payment__isnull=True)
    form = AssignC2BForm(request.POST, owner=request.user, prefix=f'c{c2b.pk}')
    if form.is_valid():
        with transaction.atomic():
            c2b.lease = form.cleaned_data['lease']
            mpesa.apply_c2b_payment(c2b)
            c2b.save()
        messages.success(request, f'Assigned KES {c2b.amount:,.0f} ({c2b.trans_id}) to {c2b.lease.tenant}.')
    else:
        messages.error(request, 'Choose a tenant to assign this payment to.')
    return redirect('payments:unmatched_list')
