import functools
import logging

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
from django.views.decorators.http import require_POST

from audit import log as audit
from notifications.models import LandlordPreferences
from payments.models import MpesaAccount
from rentals.models import Lease, Property, Tenant, Unit

from . import ratelimit
from .forms import ChangePasswordForm, ProfileForm, SignupForm
from .models import Profile, profile_for
from .verification import is_verified, mark_verified, send_verification_email, user_from_token

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
        if response.status_code in (301, 302) and request.user.is_authenticated:
            ratelimit.reset('login-user', username)
        else:
            ratelimit.hit('login-user', username, LOGIN_WINDOW)
        return response

    return wrapped


@method_decorator(throttle_login, name='dispatch')
class LoginView(auth_views.LoginView):
    """Django's login, throttled per IP and per username to slow down password guessing."""

    redirect_authenticated_user = True


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
            'title': 'Start a lease',
            'text': 'Link a tenant to a unit with the rent and due date. Rent is billed every month.',
            'done': Lease.objects.filter(unit__property__owner=user).exists(),
            'url': 'rentals:lease_create',
            'cta': 'Start lease',
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
