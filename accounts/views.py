import datetime
import functools
import logging
import time
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from audit import utils as audit
from maintenance.models import MaintenanceRequest
from notifications.models import LandlordPreferences
from notifications.utils import send_cash_receipt
from payments import mpesa
from payments.forms import HandoverForm, StkPushForm
from payments.models import MpesaAccount, MpesaTransaction, Payment
from payments.utils import HandoverError, record_expense, record_handover, staff_cash
from rentals.models import Lease, Property, Tenant, TenantNote, Unit
from rentals.utils import BillingError, add_tenant_note, last_reading, record_water_reading

from . import utils as ratelimit
from .forms import (
    CashForm, ChangePasswordForm, PhoneCodeForm, ProfileForm, RepairForm, ResetRequestForm, SignupForm, StaffExpenseForm,
    StaffForm, StaffMeterForm, TenantNoteForm,
)
from .models import Profile, StaffMember, TwoFactor, profile_for
from .utils import (
    TRUST_COOKIE, TRUST_DAYS, check_totp, is_trusted_device, new_recovery_codes, new_totp_secret, qr_svg, totp_uri,
    trust_token, use_recovery_code,
)
from .utils import (
    canonical_login, check_phone_code, find_user, is_verified, login_label, mark_verified,
    send_invite, send_phone_code, send_verification_email, user_from_token,
)

logger = logging.getLogger(__name__)

SIGNUPS_PER_IP_PER_HOUR = 5
LOGIN_FAILURES_PER_USER = 5
LOGIN_ATTEMPTS_PER_IP = 30
LOGIN_WINDOW = 15 * 60
RESETS_PER_IP_PER_HOUR = 5
RESENDS_PER_USER_PER_HOUR = 3


def _send_verification(request, user):
    try:
        send_verification_email(user)
    except Exception:  # SMTP outage shouldn't break sign-up; they can resend later
        logger.exception('Could not send verification email to user %s', user.pk)
        messages.warning(request, "We couldn't send the confirmation email just now. Use \"Resend\" in a few minutes.")


def signup(request):
    if request.user.is_authenticated:
        return redirect('rentals:dashboard')
    form = SignupForm(request.POST or None)
    if request.method == 'POST':
        ip = ratelimit.client_ip(request)
        if ratelimit.is_limited('signup', ip, SIGNUPS_PER_IP_PER_HOUR):
            return ratelimit.too_many(request, 60)
        if form.is_valid():
            ratelimit.hit('signup', ip, 60 * 60)
            user = form.save()
            Profile.objects.create(user=user, phone=form.phone, terms_accepted_at=timezone.now(),
                                   terms_version=settings.LEGAL['TERMS_VERSION'])
            if form.phone:
                prefs = LandlordPreferences.for_user(user)
                prefs.phone = form.phone
                prefs.save(update_fields=['phone'])
            login(request, user, backend='accounts.utils.EmailOrPhoneBackend')
            if form.phone:
                send_phone_code(user, form.phone, 'verify')
                messages.success(request, f'Welcome, {user.first_name}! We sent a code by SMS to confirm your number.')
                return redirect('accounts:verify_phone')
            _send_verification(request, user)
            messages.success(
                request, f'Welcome, {user.first_name}! We sent a confirmation link to {user.email}.'
            )
            return redirect('accounts:welcome')
    return render(request, 'registration/signup.html', {'form': form})


def verify_email(request, token):
    user = user_from_token(token, get_user_model())
    if user is None:
        messages.error(request, 'That confirmation link is invalid or has expired. Log in and request a new one.')
        return redirect('rentals:dashboard' if request.user.is_authenticated else 'login')
    mark_verified(user)
    messages.success(request, 'Thanks, your email is confirmed.')
    return redirect('rentals:dashboard' if request.user.is_authenticated else 'login')


@login_required
@require_POST
def resend_verification(request):
    if not request.user.email or profile_for(request.user).email_verified:
        messages.info(request, 'Your email is already confirmed.')
    elif ratelimit.hit('resend', request.user.pk, 60 * 60) > RESENDS_PER_USER_PER_HOUR:
        messages.error(request, 'Please wait a while before requesting another email.')
    else:
        _send_verification(request, request.user)
        messages.success(request, f'Sent a new confirmation link to {request.user.email}.')
    return redirect(request.POST.get('next') or 'rentals:dashboard')


def throttle_login(view):
    """Limit password guessing on any login view (the app's and the Django admin's).

    Counters are shared, so switching between login pages doesn't buy extra guesses. A login
    view redirects on success and re-shows the form on failure; that's how attempts are judged.
    """

    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method != 'POST':
            return view(request, *args, **kwargs)
        ip = ratelimit.client_ip(request)
        username = canonical_login(request.POST.get('username', ''))
        if ratelimit.hit('login-ip', ip, LOGIN_WINDOW) > LOGIN_ATTEMPTS_PER_IP or \
                ratelimit.is_limited('login-user', username, LOGIN_FAILURES_PER_USER):
            return ratelimit.too_many(request, LOGIN_WINDOW // 60)
        response = view(request, *args, **kwargs)
        # A right password counts as a success even when the two-step code is still to come.
        if response.status_code in (301, 302) and (request.user.is_authenticated
                                                   or getattr(request, 'two_factor_pending', False)):
            ratelimit.reset('login-user', username)
        else:
            ratelimit.hit('login-user', username, LOGIN_WINDOW)
        return response

    return wrapped


@method_decorator(throttle_login, name='dispatch')
class LoginView(auth_views.LoginView):
    """Django's login (email or phone number, see EmailOrPhoneBackend), throttled per IP and per login to slow
    down password guessing.

    With two-step login on (and this browser not trusted), a right password only leads to the code
    page: the person isn't logged in until the code checks out.
    """

    redirect_authenticated_user = True

    def form_valid(self, form):
        user = form.get_user()
        two_factor = TwoFactor.objects.filter(user=user, confirmed_at__isnull=False).first()
        if two_factor is None or is_trusted_device(self.request, two_factor):
            return super().form_valid(form)
        self.request.session[PENDING_KEY] = {
            'user': user.pk, 'backend': user.backend, 'at': int(time.time()), 'tries': 0,
            'next': self.get_success_url(),
        }
        self.request.two_factor_pending = True
        return redirect('accounts:login_verify')


PENDING_KEY = 'two_factor_pending'
PENDING_SECONDS = 5 * 60
CODE_TRIES = 5


def login_verify(request):
    """Second step of logging in: the 6-digit code from the authenticator app, or a recovery code."""
    pending = request.session.get(PENDING_KEY)
    if not pending or time.time() - pending['at'] > PENDING_SECONDS:
        request.session.pop(PENDING_KEY, None)
        messages.info(request, 'Please log in again.')
        return redirect('login')
    two_factor = TwoFactor.objects.filter(user_id=pending['user'], confirmed_at__isnull=False) \
        .select_related('user').first()
    if two_factor is None:  # turned off meanwhile
        request.session.pop(PENDING_KEY, None)
        return redirect('login')
    error = None
    if request.method == 'POST':
        code = request.POST.get('code', '')
        if ratelimit.hit('login-2fa', two_factor.user_id, 15 * 60) > 3 * CODE_TRIES:
            return ratelimit.too_many(request, 15)
        used_recovery = False
        ok = check_totp(two_factor, code)
        if not ok:
            ok = used_recovery = use_recovery_code(two_factor, code)
        if ok:
            user = two_factor.user
            request.session.pop(PENDING_KEY, None)
            login(request, user, backend=pending['backend'])
            if used_recovery:
                left = len(two_factor.recovery_codes)
                messages.warning(request, f'You used a recovery code; {left} left. If you lost your phone, set up '
                                          'two-step login again under Settings → Two-step login.')
            nxt = pending.get('next') or settings.LOGIN_REDIRECT_URL
            if not url_has_allowed_host_and_scheme(nxt, {request.get_host()}, request.is_secure()):
                nxt = settings.LOGIN_REDIRECT_URL
            response = redirect(nxt)
            if request.POST.get('trust'):
                response.set_cookie(TRUST_COOKIE, trust_token(two_factor),
                                    max_age=TRUST_DAYS * 86400, secure=request.is_secure(),
                                    httponly=True, samesite='Lax')
            return response
        pending['tries'] += 1
        if pending['tries'] >= CODE_TRIES:
            request.session.pop(PENDING_KEY, None)
            messages.error(request, 'Too many wrong codes. Please log in again.')
            return redirect('login')
        request.session[PENDING_KEY] = pending
        error = "That code didn't work. Check the time on your phone is correct, and use the newest code."
    return render(request, 'registration/login_verify.html', {'error': error})


class PasswordResetView(auth_views.PasswordResetView):
    """Reset by email (a link, Django's flow) or by phone number (an SMS code, then a new password)."""

    form_class = ResetRequestForm
    email_template_name = 'registration/password_reset_email.txt'
    subject_template_name = 'registration/password_reset_subject.txt'

    def post(self, request, *args, **kwargs):
        if ratelimit.hit('reset', ratelimit.client_ip(request), 60 * 60) > RESETS_PER_IP_PER_HOUR:
            return ratelimit.too_many(request, 60)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        if not form.is_phone:
            return super().form_valid(form)
        user = find_user(form.cleaned_data['email'])
        phone = getattr(getattr(user, 'profile', None), 'phone', None) if user else None
        if user is not None and phone and user.is_active and user.has_usable_password():
            send_phone_code(user, phone, 'reset')
        # The same next step whether or not the number has an account, so this can't be used to find accounts.
        self.request.session[RESET_KEY] = {'user': user.pk if phone else None, 'at': int(time.time()), 'tries': 0}
        return redirect('password_reset_phone')

    @property
    def extra_email_context(self):
        return {'APP_NAME': settings.APP_NAME, 'site_url': settings.SITE_URL.rstrip('/')}


def setup_steps(user):
    """The getting-started checklist for a landlord, in order."""
    account = MpesaAccount.objects.filter(owner=user).first()
    return [
        {
            'title': 'Add your first property',
            'text': 'Your building, with its units and their rent.',
            'done': Property.objects.filter(owner=user).exists() and Unit.objects.filter(property__owner=user).exists(),
            'url': 'rentals:property_create',
            'cta': 'Add property',
        },
        {
            'title': 'Add your tenants',
            'text': 'Their names and M-Pesa phone numbers.',
            'done': Tenant.objects.filter(owner=user).exists(),
            'url': 'rentals:tenant_create',
            'cta': 'Add tenant',
        },
        {
            'title': 'Move your first tenant in',
            'text': 'Put a tenant in a unit with their rent and rent day. Rent is then billed every month.',
            'done': Lease.objects.filter(unit__property__owner=user).exists(),
            'url': 'rentals:lease_create',
            'cta': 'Move a tenant in',
        },
        {
            'title': 'Add your Paybill or Till',
            'text': 'The number tenants pay to, so rent goes straight to you. Connect Daraja later for '
                    'payments to appear instantly.',
            'done': bool(account and account.has_pay_number),
            'url': 'payments:mpesa_settings',
            'cta': 'Add Paybill or Till',
        },
    ]


@login_required
def welcome(request):
    steps = setup_steps(request.user)
    for i, step in enumerate(steps, 1):
        step['number'] = i
    current = next((s for s in steps if not s['done']), None)
    return render(request, 'accounts/welcome.html', {
        'steps': steps,
        'current': current,
        'done_count': sum(s['done'] for s in steps),
    })


@login_required
def settings_page(request):
    """Everything a landlord sets up once, in one place (the sidebar just links here)."""
    account = MpesaAccount.objects.filter(owner=request.user).first()
    return render(request, 'accounts/settings.html', {
        'mpesa_account': account,
        'mpesa_ready': account is not None and account.is_complete(),
        'prefs': LandlordPreferences.for_user(request.user),
        'email_verified': is_verified(request.user),
        'staff_count': request.user.staff_members.filter(user__is_active=True).count(),
        'two_factor_on': TwoFactor.objects.filter(user=request.user, confirmed_at__isnull=False).exists(),
    })


PROFILE_CHANGES_PER_HOUR = 10
PASSWORD_CHANGES_PER_HOUR = 10


def _notify(email, subject, body):
    """Security notices to the landlord. A mail outage mustn't undo a change that already happened."""
    if not email:
        return
    try:
        send_mail(subject=f'{settings.APP_NAME}: {subject}', message=f'{body}\n\n{settings.APP_NAME}',
                  from_email=None, recipient_list=[email])
    except Exception:
        logger.exception('Could not send security notice "%s"', subject)


@login_required
def profile_edit(request):
    prefs = LandlordPreferences.for_user(request.user)
    form = ProfileForm(request.POST or None, instance=request.user, prefs=prefs)
    if request.method == 'POST':
        if ratelimit.hit('profile', request.user.pk, 60 * 60) > PROFILE_CHANGES_PER_HOUR:
            messages.error(request, 'Too many changes in a short time. Please try again in an hour.')
            return redirect('accounts:profile')
        if form.is_valid():
            old_email, email_changed, phone_changed = form.original_email, form.email_changed, form.phone_changed
            changes = audit.form_changes(form)
            changes.pop('Current password', None)  # typed to confirm, not changed
            user = form.save()
            audit.record(owner=user, actor=user, action='account.profile_changed',
                         summary='Updated profile details.', changes=changes)
            notes = []
            if email_changed and old_email:
                _notify(old_email, 'your email was changed',
                        f'Hi {user.first_name or "there"},\n\nThe email on your account was changed to '
                        f'{user.email or "(none)"}. If this wasn\'t you, reply to this email or contact support right away.')
            if email_changed and user.email:
                _send_verification(request, user)
                notes.append(f'We sent a link to {user.email}: open it to confirm your new email.')
            if phone_changed and form.cleaned_data['phone']:
                send_phone_code(user, form.cleaned_data['phone'], 'verify')
                messages.success(request, 'Saved. We sent a code by SMS to confirm your new number.')
                return redirect('accounts:verify_phone')
            messages.success(request, ' '.join(['Saved.', *notes]))
            return redirect('accounts:settings')
    return render(request, 'accounts/profile.html', {'form': form})


@method_decorator(login_required, name='dispatch')
class ChangePasswordView(auth_views.PasswordChangeView):
    """Change password. Other devices are signed out (Django does this); this one stays signed in."""

    form_class = ChangePasswordForm
    template_name = 'form_page.html'
    success_url = reverse_lazy('accounts:settings')
    extra_context = {
        'page_title': 'Change password',
        'intro': 'You stay signed in here. Anywhere else you are signed in will be signed out.',
        'submit_label': 'Change password',
        'cancel_url': reverse_lazy('accounts:settings'),
    }

    def get_success_url(self):
        if getattr(self.request, 'viewer', None):  # read-only access: Settings is the landlord's
            return reverse('rentals:dashboard')
        return super().get_success_url()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if getattr(self.request, 'viewer', None):
            ctx['cancel_url'] = reverse('rentals:dashboard')
        return ctx

    def post(self, request, *args, **kwargs):
        if ratelimit.hit('password-change', request.user.pk, 60 * 60) > PASSWORD_CHANGES_PER_HOUR:
            messages.error(request, 'Too many attempts. Please try again in an hour.')
            return redirect(request.path)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.user
        audit.record(owner=user, actor=user, action='account.password_changed', summary='Changed password.')
        _notify(user.email, 'your password was changed',
                f'Hi {user.first_name or "there"},\n\nYour password was just changed. If this wasn\'t you, '
                'reset it now using "Forgot password?" on the login page, and contact support.')
        messages.success(self.request, 'Password changed.')
        return response


@login_required
def two_factor_settings(request):
    """Turn two-step login on (scan a QR code, confirm a code, save recovery codes) or off."""
    user = request.user
    owner = request.staff.owner if getattr(request, 'staff', None) else user
    two_factor = TwoFactor.objects.filter(user=user).first()
    ctx = {'base_template': 'staff/base.html' if getattr(request, 'staff', None) else 'app_base.html'}

    if request.method == 'POST':
        if ratelimit.hit('2fa-settings', user.pk, 15 * 60) > 10:
            return ratelimit.too_many(request, 15)
        action = request.POST.get('action')
        code = request.POST.get('code', '')
        if action == 'start' and not (two_factor and two_factor.enabled):
            TwoFactor.objects.update_or_create(user=user, defaults={
                'secret': new_totp_secret(), 'confirmed_at': None, 'last_step': 0, 'recovery_codes': []})
            return redirect(request.path)
        if action == 'confirm' and two_factor and not two_factor.enabled:
            if check_totp(two_factor, code):
                two_factor.confirmed_at = timezone.now()
                two_factor.trust_version += 1
                two_factor.save(update_fields=['confirmed_at', 'trust_version'])
                ctx['new_codes'] = new_recovery_codes(two_factor)
                audit.record(owner=owner, actor=user, action='account.two_factor_on', summary='Turned on two-step login.')
                _notify(user.email, 'two-step login is on',
                        f'Hi {user.first_name or "there"},\n\nTwo-step login is now on for your account: '
                        'logging in needs a code from your authenticator app as well as your password.')
            else:
                ctx['error'] = "That code didn't work. Make sure you scanned the code on this page, then try the newest code."
        elif action == 'codes' and two_factor and two_factor.enabled:
            if check_totp(two_factor, code):
                ctx['new_codes'] = new_recovery_codes(two_factor)
                audit.record(owner=owner, actor=user, action='account.two_factor_codes',
                             summary='Made new two-step recovery codes.')
            else:
                ctx['error'] = "That code didn't work."
        elif action == 'disable' and two_factor and two_factor.enabled:
            if not user.check_password(request.POST.get('password', '')):
                ctx['error'] = "That password isn't right."
            elif not (check_totp(two_factor, code) or use_recovery_code(two_factor, code)):
                ctx['error'] = "That code didn't work."
            else:
                two_factor.delete()
                audit.record(owner=owner, actor=user, action='account.two_factor_off', summary='Turned off two-step login.')
                _notify(user.email, 'two-step login was turned off',
                        f'Hi {user.first_name or "there"},\n\nTwo-step login was just turned off for your account. '
                        "If this wasn't you, change your password now and contact support.")
                messages.success(request, 'Two-step login is off.')
                return redirect(request.path)
        two_factor = TwoFactor.objects.filter(user=user).first()

    ctx['two_factor'] = two_factor
    if two_factor and not two_factor.enabled:
        ctx['qr'] = qr_svg(totp_uri(user, two_factor.secret))
        ctx['manual_key'] = ' '.join(two_factor.secret[i:i + 4] for i in range(0, len(two_factor.secret), 4))
    return render(request, 'accounts/two_factor.html', ctx)


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


# --- Staff area (caretakers): prompt tenants to pay, record cash, log repairs ------------------------------------------------------------------------

@staff_required
def staff_home(request):
    staff = request.staff
    show_all = request.GET.get('show') == 'all'
    q = request.GET.get('q', '').strip()
    leases = staff.leases().select_related('tenant', 'unit__property').order_by('unit__property__name', 'unit__sort_key')
    if q:
        leases = leases.filter(Q(tenant__full_name__icontains=q) | Q(unit__label__icontains=q))
    last_paid = dict(Payment.objects.filter(lease__in=leases).values_list('lease').annotate(last=Max('paid_at')))
    prompts = {}
    for txn in MpesaTransaction.objects.filter(lease__in=leases, created_at__gte=timezone.now() - RECENT_PROMPT):
        prompts.setdefault(txn.lease_id, txn)  # newest first
    notes = {}
    for note in TenantNote.objects.filter(lease__in=leases).order_by('-created_at'):
        notes.setdefault(note.lease_id, note)  # the latest one
    rows = []
    for lease in leases:
        balance = lease.balance
        if balance > 0 or show_all or q:
            rows.append({'lease': lease, 'balance': balance, 'last_paid': last_paid.get(lease.pk),
                         'prompt': prompts.get(lease.pk), 'note': notes.get(lease.pk)})
    rows.sort(key=lambda r: -r['balance'])
    account = MpesaAccount.objects.filter(owner=staff.owner).first()
    return render(request, 'staff/home.html', {
        'rows': rows, 'show_all': show_all, 'q': q, 'cash': staff_cash(staff),
        'mpesa_ready': account is not None and account.is_complete(),
        'open_repairs': MaintenanceRequest.objects.filter(property__in=staff.looked_after(), status__in=OPEN_REPAIRS).count(),
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
                                                 recorded_by=request.user,
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
def handover(request):
    """A caretaker records passing collected cash on to the landlord; the landlord then confirms it."""
    cash = staff_cash(request.staff)
    form = HandoverForm(request.POST or None, initial={'amount': cash['to_hand_over'] or None})
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            record_handover(request.staff, data['amount'], data['method'], data['reference'], data['note'],
                            request.user)
        except HandoverError as exc:
            form.add_error('amount', str(exc))
        else:
            _tell_landlord_about_handover(request.staff, data['amount'])
            messages.success(request, f"Recorded. {request.staff.owner.first_name or 'The landlord'} will confirm "
                                      f"receiving KES {data['amount']:,.0f}.")
            return redirect('staff:home')
    return render(request, 'staff/form.html', {
        'form': form, 'title': 'Hand over cash',
        'intro': f"You're holding KES {cash['holding']:,.0f}"
                 + (f" (KES {cash['pending']:,.0f} already handed over, waiting for confirmation)" if cash['pending'] else '')
                 + f". {request.staff.owner.first_name or 'The landlord'} confirms when they receive it.",
        'submit': 'Record handover',
        'confirm': 'Record handing over KES {amount}? The landlord will be asked to confirm it.',
    })


def _tell_landlord_about_handover(member, amount):
    owner = member.owner
    if not owner.email:
        return
    try:
        send_mail(f'{settings.APP_NAME}: {member.name} handed over KES {amount:,.0f}',
                  f'Hi {owner.first_name or "there"},\n\n{member.name} says they handed over KES {amount:,.0f} cash. '
                  f'Please confirm you received it: {settings.SITE_URL}{reverse("payments:cash_with_staff")}\n\n'
                  f'{settings.APP_NAME}', None, [owner.email])
    except Exception:
        logger.exception('Could not email %s about a cash handover', owner.pk)


def staff_can(permission):
    """Only caretakers whose landlord ticked `permission` for them (Settings → Staff)."""

    def decorator(view):
        @wraps(view)
        @staff_required
        def wrapper(request, *args, **kwargs):
            if not getattr(request.staff, permission):
                messages.error(request, "Your landlord hasn't turned this on for you.")
                return redirect('staff:home')
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


@staff_can('can_read_meters')
def staff_meters(request):
    """Units the caretaker looks after, with the last water meter reading of each."""
    units = request.staff.units().select_related('property').order_by('property__name', 'sort_key')
    rows = [{'unit': u, 'last': last_reading(u)} for u in units]
    return render(request, 'staff/meters.html', {'rows': rows,
                                                'no_rate': not any(u.property.water_rate for u in units)})


@staff_can('can_read_meters')
def staff_meter_reading(request, pk):
    from listings.utils import prepare_photo

    unit = get_object_or_404(request.staff.units().select_related('property'), pk=pk)
    last = last_reading(unit)
    form = StaffMeterForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        if data['reading_date'] > timezone.localdate():
            form.add_error('reading_date', "That's in the future.")
        else:
            try:
                photo = prepare_photo(data['photo']) if data['photo'] else None
                reading = record_water_reading(unit, data['reading_date'], data['value'], request.user, photo)
            except ValidationError as exc:
                form.add_error('photo', exc)
            except BillingError as exc:
                form.add_error('value', str(exc).split(': ', 1)[-1])
            else:
                billed = f' Billed KES {reading.charge.amount:,.0f} for water.' if reading.charge else ''
                _record(request, 'water.billed', f'{request.staff.name} (staff) entered meter reading '
                                                 f'{reading.value} for {unit}.{billed}')
                messages.success(request, f'Saved the reading for {unit.label}.{billed}')
                return redirect('staff:meters')
    intro = (f'Last reading: {last.value} on {last.reading_date:%d %b %Y}.' if last else
             'No reading yet: this first one is the starting point, nothing is billed.')
    return render(request, 'staff/form.html', {
        'form': form, 'title': f'Meter reading · {unit.label}', 'intro': intro, 'submit': 'Save reading',
        'cancel': reverse('staff:meters'), 'multipart': True,
    })


@staff_can('can_record_spending')
def staff_spending(request):
    """Record money spent from the cash the caretaker holds; the landlord approves it."""
    from payments.models import StaffExpense

    cash = staff_cash(request.staff)
    form = StaffExpenseForm(request.POST or None, request.FILES or None, member=request.staff)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            record_expense(request.staff, data['amount'], data['description'], data['unit'], data['receipt'],
                           data['spent_on'], request.user)
        except HandoverError as exc:
            form.add_error('amount', str(exc))
        except ValidationError as exc:
            form.add_error('receipt', exc)
        else:
            messages.success(request, f"Recorded. {request.staff.owner.first_name or 'The landlord'} will approve it.")
            return redirect('staff:spending')
    return render(request, 'staff/spending.html', {
        'form': form, 'cash': cash,
        'recent': StaffExpense.objects.filter(staff=request.staff).select_related('unit')[:15],
    })


@staff_can('can_add_notes')
def staff_note(request, pk):
    """A note on a tenant: what they said, or a promise to pay by a date."""
    lease = _lease(request, pk)
    form = TenantNoteForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        add_tenant_note(lease, request.user, **form.cleaned_data)
        messages.success(request, f'Note saved for {lease.tenant}.')
        return redirect('staff:home')
    return render(request, 'staff/note.html', {'form': form, 'lease': lease, 'notes': lease.notes.all()[:10]})


@staff_required
def repairs(request):
    items = (MaintenanceRequest.objects.filter(property__in=request.staff.looked_after(), status__in=OPEN_REPAIRS)
             .select_related('property', 'unit__property', 'reported_by').order_by('-created_at'))
    return render(request, 'staff/repairs.html', {'repairs': items})


@staff_required
def repair_form(request, pk=None):
    instance = None
    if pk is not None:
        instance = get_object_or_404(MaintenanceRequest, pk=pk, property__in=request.staff.looked_after())
    form = RepairForm(request.POST or None, instance=instance, member=request.staff)
    if request.method == 'POST' and form.is_valid():
        repair = form.save()
        verb = 'updated' if instance else 'logged'
        if form.cleaned_data.get('notify_tenants'):
            from maintenance.utils import notify_tenants

            sent = notify_tenants(repair, created=instance is None)
            if sent:
                messages.info(request, f'Texted {sent} tenant{"s" if sent != 1 else ""} about it.')
        _record(request, f'repair.{verb}', f'{request.staff.name} (staff) {verb} repair "{repair.title}" '
                                           f'({repair.where}).')
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
                         summary=f'Added staff member {member.name} ({login_label(member.user)}).')
            messages.success(request, f'Added {member.name}. They log in with their phone number ({member.phone}) '
                                      'and the password you set.')
        return redirect('staff:manage')
    return render(request, 'form_page.html', {
        'form': form, 'page_title': f'Edit {member.name}' if member else 'Add staff',
        'intro': None if member else 'A caretaker or agent. They can ask tenants to pay by M-Pesa and see '
                                     'whether it arrived, record cash and log repairs. They never see your '
                                     'totals, reports or settings.',
        'submit_label': 'Save' if member else 'Add staff member', 'cancel_url': reverse('staff:manage'),
    })


@login_required
def accept_terms(request):
    """The terms changed since this landlord accepted them: show what's new and ask them to accept."""
    from .utils import TERMS_SESSION_KEY

    current = settings.LEGAL['TERMS_VERSION']
    nxt = request.GET.get('next') or request.POST.get('next') or ''
    if not url_has_allowed_host_and_scheme(nxt, {request.get_host()}, request.is_secure()) or nxt.startswith('/terms'):
        nxt = reverse('rentals:dashboard')
    error = ''
    if request.method == 'POST':
        if request.POST.get('accept'):
            profile = profile_for(request.user)
            profile.terms_version, profile.terms_accepted_at = current, timezone.now()
            profile.save(update_fields=['terms_version', 'terms_accepted_at'])
            request.session[TERMS_SESSION_KEY] = current
            audit.record(owner=request.user, actor=request.user, action='account.terms_accepted',
                         summary=f'Accepted the terms of service ({current}).')
            return redirect(nxt)
        error = 'Tick the box to accept the updated terms, or log out.'
    return render(request, 'accounts/accept_terms.html', {'next': nxt, 'error': error,
                                                          'updated': settings.LEGAL['UPDATED']})


# --- Password reset by phone, and confirming a phone number ---------------------------------------------

RESET_KEY = 'phone_reset'
RESET_SECONDS = 10 * 60


def password_reset_phone(request):
    """Second step of a reset by phone: the SMS code and a new password."""
    pending = request.session.get(RESET_KEY)
    if not pending or time.time() - pending['at'] > RESET_SECONDS:
        request.session.pop(RESET_KEY, None)
        messages.info(request, 'Please start the password reset again.')
        return redirect('password_reset')
    user = get_user_model().objects.filter(pk=pending['user']).first() if pending['user'] else None
    code_form = PhoneCodeForm(request.POST or None)
    password_form = SetPasswordForm(user or get_user_model()(), request.POST or None)
    if request.method == 'POST' and code_form.is_valid() and password_form.is_valid():
        if user is not None and check_phone_code(user, 'reset', code_form.cleaned_data['code']):
            password_form.save()
            request.session.pop(RESET_KEY, None)
            audit.record(owner=user, actor=user, action='account.password_reset',
                         summary='Reset the password by SMS code.')
            messages.success(request, 'Your password is changed. Log in with your new password.')
            return redirect('login')
        pending['tries'] += 1
        if pending['tries'] >= CODE_TRIES:
            request.session.pop(RESET_KEY, None)
            messages.error(request, 'Too many wrong codes. Please start again.')
            return redirect('password_reset')
        request.session[RESET_KEY] = pending
        code_form.add_error('code', "That code isn't right, or it has expired.")
    return render(request, 'registration/password_reset_phone.html',
                  {'code_form': code_form, 'password_form': password_form})


@login_required
def verify_phone(request):
    """Confirm the account's phone number with the code we texted."""
    profile = profile_for(request.user)
    if not profile.phone:
        return redirect('accounts:profile')
    if profile.phone_verified:
        messages.info(request, 'Your phone number is already confirmed.')
        return redirect('rentals:dashboard')
    form = PhoneCodeForm(request.POST or None)
    if request.method == 'POST' and request.POST.get('action') == 'resend':
        if ratelimit.hit('resend', request.user.pk, 60 * 60) > RESENDS_PER_USER_PER_HOUR:
            messages.error(request, 'Please wait a while before requesting another code.')
        else:
            send_phone_code(request.user, profile.phone, 'verify')
            messages.success(request, 'Sent a new code.')
        return redirect('accounts:verify_phone')
    if request.method == 'POST' and form.is_valid():
        if ratelimit.hit('verify-phone', request.user.pk, 15 * 60) > 3 * CODE_TRIES:
            return ratelimit.too_many(request, 15)
        if check_phone_code(request.user, 'verify', form.cleaned_data['code']):
            profile.phone_verified_at = timezone.now()
            profile.save(update_fields=['phone_verified_at'])
            messages.success(request, 'Thanks, your phone number is confirmed.')
            return redirect('accounts:welcome' if not Property.objects.filter(owner=request.user).exists()
                            else 'rentals:dashboard')
        form.add_error('code', "That code isn't right, or it has expired. Use the newest one, or send a new code.")
    return render(request, 'accounts/verify_phone.html', {'form': form, 'phone': f'0{profile.phone[3:]}'})


# --- People with access (Settings → People with access) ----------------------------------------------------

@landlord_required
def viewer_list(request):
    from .models import ViewerAccess
    from .utils import invite_url

    viewers = list(ViewerAccess.objects.filter(owner=request.user).select_related('user', 'user__profile'))
    for v in viewers:
        v.invite_link = None if v.joined else invite_url(v)  # to copy into WhatsApp if the SMS/email doesn't arrive
    return render(request, 'accounts/viewers.html', {'viewers': viewers})


def _invite_message(access, sent):
    how = {'email': f'emailed {access.user.email}', 'sms': 'sent by SMS'}.get(sent)
    if how:
        return f'Invite {how}. {access.name} chooses a password from the link, then logs in.'
    return f"We couldn't send {access.name}'s invite: copy their invite link below and send it on WhatsApp."


@landlord_required
def viewer_edit(request, pk=None):
    from .forms import ViewerForm
    from .models import ViewerAccess

    access = get_object_or_404(ViewerAccess, pk=pk, owner=request.user) if pk else None
    form = ViewerForm(request.POST or None, owner=request.user, access=access)
    if request.method == 'POST' and form.is_valid():
        access = form.save()
        who = f'{access.name} ({login_label(access.user)})'
        level = access.level.lower()
        if pk:
            audit.record(owner=request.user, actor=request.user, action='access.updated',
                         summary=f'Updated access for {who}: {level}'
                                 + ('' if access.user.is_active else ', can no longer log in') + '.')
            messages.success(request, f'Saved {access.name}.')
        else:
            audit.record(owner=request.user, actor=request.user, action='access.added',
                         summary=f'Gave {who} access to the account ({level}).')
            if access.joined:
                messages.success(request, f'{access.name} can now log in with {login_label(access.user)} and the '
                                          'password you set.')
            else:
                sent = send_invite(access)
                (messages.success if sent else messages.warning)(request, _invite_message(access, sent))
        return redirect('accounts:viewers')
    return render(request, 'form_page.html', {
        'form': form, 'page_title': f'Edit {access.name}' if access else 'Invite someone to your account',
        'intro': None if access else 'A co-owner, family member or accountant. They see your dashboard, tenants, '
                                     'payments, expenses and reports, and can download statements and CSVs. '
                                     'Choose whether they can also make changes.',
        'submit_label': 'Save' if access else 'Send invite', 'cancel_url': reverse('accounts:viewers'),
    })


@landlord_required
@require_POST
def viewer_resend(request, pk):
    from .models import ViewerAccess

    access = get_object_or_404(ViewerAccess, pk=pk, owner=request.user)
    if access.joined:
        messages.info(request, f'{access.name} has already joined.')
    else:
        sent = send_invite(access)
        (messages.success if sent else messages.warning)(request, _invite_message(access, sent))
    return redirect('accounts:viewers')


def invite_accept(request, uidb64, token):
    """The invited person chooses their password, then they're logged straight in."""
    from django.utils.encoding import force_str
    from django.utils.http import urlsafe_base64_decode

    from .models import viewer_for
    from .utils import invite_tokens

    try:
        user = get_user_model().objects.get(pk=force_str(urlsafe_base64_decode(uidb64)), is_active=True)
    except (ValueError, TypeError, OverflowError, get_user_model().DoesNotExist):
        user = None
    access = viewer_for(user) if user else None
    if access is None or not invite_tokens.check_token(user, token):
        return render(request, 'accounts/invite.html', {'invalid': True}, status=400)
    form = SetPasswordForm(user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        login(request, user, backend='accounts.utils.EmailOrPhoneBackend')
        audit.record(owner=access.owner, actor=user, action='access.joined',
                     summary=f'{access.name} accepted the invite and logged in.')
        messages.success(request, f"Welcome, {user.first_name or 'there'}! Log in next time with "
                                  f'{login_label(user)} and your new password.')
        return redirect('rentals:dashboard')
    return render(request, 'accounts/invite.html', {'form': form, 'access': access})

