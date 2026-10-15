import functools
import logging
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from audit import utils as audit
from notifications.models import LandlordPreferences
from payments.models import MpesaAccount
from rentals.models import Lease, Property, Tenant, Unit

from . import utils as ratelimit
from .forms import ChangePasswordForm, ProfileForm, SignupForm
from .models import Profile, TwoFactor, profile_for
from .utils import (
    TRUST_COOKIE, TRUST_DAYS, check_totp, is_trusted_device, new_recovery_codes, new_totp_secret, qr_svg, totp_uri,
    trust_token, use_recovery_code,
)
from .utils import is_verified, mark_verified, send_verification_email, user_from_token

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
            Profile.objects.create(user=user, terms_accepted_at=timezone.now(),
                                   terms_version=settings.LEGAL['TERMS_VERSION'])
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
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
    if is_verified(request.user):
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
        username = request.POST.get('username', '')
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
    """Django's login, throttled per IP and per username to slow down password guessing.

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
    email_template_name = 'registration/password_reset_email.txt'
    subject_template_name = 'registration/password_reset_subject.txt'

    def post(self, request, *args, **kwargs):
        if ratelimit.hit('reset', ratelimit.client_ip(request), 60 * 60) > RESETS_PER_IP_PER_HOUR:
            return ratelimit.too_many(request, 60)
        return super().post(request, *args, **kwargs)

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
            'title': 'Connect M-Pesa',
            'text': 'Your own Paybill or Till, so rent goes straight to you and is recorded automatically.',
            'done': bool(account and account.is_complete() and account.verified_at),
            'url': 'payments:mpesa_settings',
            'cta': 'Connect M-Pesa',
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
            old_email, email_changed = form.original_email, form.email_changed
            changes = audit.form_changes(form)
            changes.pop('Current password', None)  # typed to confirm, not changed
            user = form.save()
            audit.record(owner=user, actor=user, action='account.profile_changed',
                         summary='Updated profile details.', changes=changes)
            if email_changed:
                profile = profile_for(user)
                profile.email_verified_at = None
                profile.save(update_fields=['email_verified_at'])
                _send_verification(request, user)
                _notify(old_email, 'your email was changed',
                        f'Hi {user.first_name or user.username},\n\nThe email on your account was changed to '
                        f'{user.email}. If this wasn\'t you, reply to this email or contact support right away.')
                messages.success(request, f'Saved. We sent a link to {user.email}: open it to confirm your new email.')
            else:
                messages.success(request, 'Saved.')
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
                f'Hi {user.first_name or user.username},\n\nYour password was just changed. If this wasn\'t you, '
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
                        f'Hi {user.first_name or user.username},\n\nTwo-step login is now on for your account: '
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
                        f'Hi {user.first_name or user.username},\n\nTwo-step login was just turned off for your account. '
                        "If this wasn't you, change your password now and contact support.")
                messages.success(request, 'Two-step login is off.')
                return redirect(request.path)
        two_factor = TwoFactor.objects.filter(user=user).first()

    ctx['two_factor'] = two_factor
    if two_factor and not two_factor.enabled:
        ctx['qr'] = qr_svg(totp_uri(user, two_factor.secret))
        ctx['manual_key'] = ' '.join(two_factor.secret[i:i + 4] for i in range(0, len(two_factor.secret), 4))
    return render(request, 'accounts/two_factor.html', ctx)
