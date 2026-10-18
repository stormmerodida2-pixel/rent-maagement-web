"""Helpers for accounts: rate limiting, email verification, two-step login, and keeping staff in their area."""

import hashlib
import logging
import re
import secrets
import time

import pyotp
import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core import signing
from django.core.cache import cache
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import profile_for, staff_for

logger = logging.getLogger(__name__)


# --- Rate limiting -----------------------------------------------------------------------------------

# Small fixed-window rate limiter on Django's cache.

def client_ip(request):
    """The visitor's IP. Behind TRUSTED_PROXY_COUNT proxies, read it from X-Forwarded-For.

    Each proxy appends the address it received the request from, so the client is the entry
    TRUSTED_PROXY_COUNT places from the right. Anything further left was supplied by the client
    and can't be trusted (it could be forged to dodge the limits).
    """
    proxies = settings.TRUSTED_PROXY_COUNT
    if proxies > 0:
        hops = [h.strip() for h in request.META.get('HTTP_X_FORWARDED_FOR', '').split(',') if h.strip()]
        if len(hops) >= proxies:
            return hops[-proxies]
    return request.META.get('REMOTE_ADDR', 'unknown')


def _key(scope, ident):
    return f'rl:{scope}:{hashlib.sha256(str(ident).lower().encode()).hexdigest()[:32]}'


def is_limited(scope, ident, limit):
    return (cache.get(_key(scope, ident)) or 0) >= limit


def hit(scope, ident, window):
    """Record one attempt. Returns the count within the current window."""
    key = _key(scope, ident)
    if cache.add(key, 1, window):
        return 1
    try:
        return cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.add(key, 1, window)
        return 1


def reset(scope, ident):
    cache.delete(_key(scope, ident))


def too_many(request, minutes):
    return render(request, '429.html', {'minutes': minutes}, status=429)


# --- Email verification ------------------------------------------------------------------------------

# Email verification links: signed, time-limited, and invalidated if the email changes.

SALT = 'accounts.verify-email'
MAX_AGE = 60 * 60 * 24 * 3  # 3 days


def make_token(user):
    return signing.dumps({'u': user.pk, 'e': user.email.lower()}, salt=SALT)


def user_from_token(token, User):
    """Return the user a valid token belongs to, or None."""
    try:
        data = signing.loads(token, salt=SALT, max_age=MAX_AGE)
    except signing.BadSignature:  # includes SignatureExpired
        return None
    user = User.objects.filter(pk=data.get('u')).first()
    if user is None or user.email.lower() != data.get('e'):
        return None
    return user


def send_verification_email(user):
    url = settings.SITE_URL.rstrip('/') + reverse('accounts:verify_email', args=[make_token(user)])
    context = {'user': user, 'url': url, 'APP_NAME': settings.APP_NAME}
    send_mail(
        subject=f'Confirm your email for {settings.APP_NAME}',
        message=render_to_string('accounts/email/verify_email.txt', context),
        from_email=None,
        recipient_list=[user.email],
    )


def mark_verified(user):
    profile = profile_for(user)
    if profile.email_verified_at is None:
        profile.email_verified_at = timezone.now()
        profile.save(update_fields=['email_verified_at'])
    return profile


def is_verified(user):
    """The person has proved they own the account's email or phone number."""
    if not user.is_authenticated:
        return False
    profile = profile_for(user)
    return profile.email_verified or profile.phone_verified


# --- Logging in with an email or phone number ---------------------------------------------------------

def canonical_login(identifier):
    """The same person typing their login differently (case, 07… vs +254…) counts as one, e.g. for limits."""
    from rentals.utils import normalize_phone

    value = (identifier or '').strip()
    if '@' in value:
        return value.lower()
    try:
        return normalize_phone(value)
    except ValueError:
        return value


def find_user(identifier):
    """The account for an email, a phone number (any common format) or, for older accounts, a username."""
    from rentals.utils import normalize_phone

    User = get_user_model()
    value = (identifier or '').strip()
    if not value:
        return None
    if '@' in value:
        matches = list(User.objects.filter(email__iexact=value)[:2])
        if len(matches) == 1:
            return matches[0]
    try:
        phone = normalize_phone(value)
    except ValueError:
        phone = None
    if phone:
        user = User.objects.filter(profile__phone=phone).first()
        if user:
            return user
    return User.objects.filter(username=value).first()


class EmailOrPhoneBackend(ModelBackend):
    """Log in with an email address or phone number and password (or an older account's username)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        user = find_user(username)
        if user is None:
            get_user_model()().set_password(password)  # same work as a real check, so timing reveals nothing
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None


def new_username():
    """Accounts still need a unique username inside Django; nobody sees or types it."""
    User = get_user_model()
    while True:
        name = f'user-{secrets.token_hex(5)}'
        if not User.objects.filter(username=name).exists():
            return name


def login_label(user):
    """How to show someone's login: their email, else phone, else (older accounts) username."""
    if user.email:
        return user.email
    phone = getattr(getattr(user, 'profile', None), 'phone', None)
    return f'0{phone[3:]}' if phone else user.username


# --- SMS codes (verifying a phone, resetting a password by phone) ---------------------------------------

CODE_TTL = 10 * 60
CODE_TRIES = 5


def _code_key(purpose, user_id):
    return f'phone-code:{purpose}:{user_id}'


def send_phone_code(user, phone, purpose):
    """Text a 6-digit code to `phone`. Only its hash is kept, for 10 minutes. Returns the SmsMessage."""
    from notifications.utils import send_sms

    code = f'{secrets.randbelow(10 ** 6):06d}'
    cache.set(_code_key(purpose, user.pk), {'hash': _hash_code(code), 'tries': 0}, CODE_TTL)
    what = 'confirm your phone number' if purpose == 'verify' else 'reset your password'
    return send_sms(phone, f'{code} is your {settings.APP_NAME} code to {what}. It expires in 10 minutes. '
                           'Never share it with anyone.')


def check_phone_code(user, purpose, code):
    """True once for the right code; a code is used up after 5 wrong tries or when it's accepted."""
    key = _code_key(purpose, user.pk)
    stored = cache.get(key)
    if not stored:
        return False
    if _hash_code(re.sub(r'\D', '', code or '')) == stored['hash']:
        cache.delete(key)
        return True
    stored['tries'] += 1
    if stored['tries'] >= CODE_TRIES:
        cache.delete(key)
    else:
        cache.set(key, stored, CODE_TTL)
    return False


# --- Two-step login ---------------------------------------------------------------------------------

TOTP_STEP = 30
RECOVERY_CODE_COUNT = 10
TRUST_COOKIE = 'trusted_device'
TRUST_DAYS = 30
TRUST_SALT = 'accounts.trusted-device'


def new_totp_secret():
    return pyotp.random_base32()


def totp_uri(user, secret):
    """What the QR code holds: authenticator apps show it as "Rentipay (their email or phone)"."""
    return pyotp.TOTP(secret).provisioning_uri(name=login_label(user), issuer_name=settings.APP_NAME)


def qr_svg(text):
    """An inline SVG QR code (no image file, no JavaScript)."""
    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    return image.to_string(encoding='unicode')


def _clean_code(code):
    return ''.join((code or '').split()).replace('-', '').lower()


def check_totp(two_factor, code, now=None):
    """True if `code` is the current 6-digit code (or the one just before/after, for clock drift),
    and it hasn't been used already. Records it so the same code can't log in twice."""
    code = _clean_code(code)
    if not (len(code) == 6 and code.isdigit()):
        return False
    totp = pyotp.TOTP(two_factor.secret)
    now = time.time() if now is None else now
    step = int(now // TOTP_STEP)
    for offset in (0, -1, 1):
        candidate = step + offset
        if candidate > two_factor.last_step and secrets.compare_digest(totp.at(candidate * TOTP_STEP), code):
            two_factor.last_step = candidate
            two_factor.save(update_fields=['last_step'])
            return True
    return False


def _hash_code(code):
    return hashlib.sha256(_clean_code(code).encode()).hexdigest()


def new_recovery_codes(two_factor):
    """Replace the recovery codes. Returns the new codes to show once; only their hashes are kept."""
    codes = [f'{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}' for _ in range(RECOVERY_CODE_COUNT)]
    two_factor.recovery_codes = [_hash_code(c) for c in codes]
    two_factor.save(update_fields=['recovery_codes'])
    return codes


def use_recovery_code(two_factor, code):
    """True (and the code is used up) if `code` is one of the unused recovery codes."""
    digest = _hash_code(code)
    if len(_clean_code(code)) == 12 and digest in two_factor.recovery_codes:
        two_factor.recovery_codes = [h for h in two_factor.recovery_codes if h != digest]
        two_factor.save(update_fields=['recovery_codes'])
        return True
    return False


def trust_token(two_factor):
    return signing.dumps({'u': two_factor.user_id, 'v': two_factor.trust_version}, salt=TRUST_SALT)


def is_trusted_device(request, two_factor):
    """True if this browser was trusted for this account within the last 30 days (and not revoked since)."""
    try:
        data = signing.loads(request.COOKIES.get(TRUST_COOKIE, ''), salt=TRUST_SALT, max_age=TRUST_DAYS * 86400)
    except signing.BadSignature:
        return False
    return data.get('u') == two_factor.user_id and data.get('v') == two_factor.trust_version


# --- Staff area middleware -----------------------------------------------------------------------------------

# Everything else (dashboard, reports, payments, settings…) is off limits to staff accounts.
STAFF_ALLOWED = ('/staff/', '/logout/', '/login/', '/static/', '/sw.js', '/manifest.webmanifest', '/offline/',
                 '/privacy/', '/terms/', '/help/', '/healthz/', '/robots.txt', '/t/', '/private/')


class StaffAreaMiddleware:
    """Keep staff accounts inside the staff area: deny by default, so new landlord pages are covered too."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.staff = staff_for(request.user)
        if request.staff is not None and not request.path.startswith(STAFF_ALLOWED):
            return redirect('staff:home')
        return self.get_response(request)


# --- Updated terms -------------------------------------------------------------------------------------

TERMS_SESSION_KEY = 'terms_version'
# Still reachable before accepting: the terms themselves, getting out, and background requests.
TERMS_OPEN_PATHS = ('/terms', '/privacy', '/help/', '/logout/', '/login/', '/static/', '/media/', '/sw.js', '/manifest.webmanifest',
                    '/offline/', '/healthz/', '/robots.txt', '/t/', '/live/', '/payments/hooks/', '/billing/hooks/')


class UpdatedTermsMiddleware:
    """A landlord who accepted an older version of the terms is asked to accept the current one before going on.

    Accounts with no recorded acceptance (created by an administrator) aren't interrupted.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and not user.is_superuser and getattr(request, 'staff', None) is None \
                and getattr(request, 'impersonator', None) is None \
                and not request.path.startswith(TERMS_OPEN_PATHS):  # support never accepts terms for a landlord
            current = settings.LEGAL['TERMS_VERSION']
            if request.session.get(TERMS_SESSION_KEY) != current:
                accepted = profile_for(user).terms_version
                if accepted and accepted != current:
                    return redirect(f"{reverse('accounts:accept_terms')}?next={request.get_full_path()}"
                                    if request.method == 'GET' else reverse('accounts:accept_terms'))
                request.session[TERMS_SESSION_KEY] = current
        return self.get_response(request)



# --- Read-only access (co-owners, accountants) ------------------------------------------------------------

# Their own login: password, two-step login and getting in and out stay theirs, not the landlord's.
VIEWER_OWN_PATHS = ('/settings/password/', '/settings/two-step/', '/login/', '/logout/', '/terms/accept/',
                    '/static/', '/sw.js', '/manifest.webmanifest', '/offline/', '/help/', '/privacy/', '/terms/')
# Never for people with access, even those who can make changes: the landlord's settings, billing, M-Pesa keys,
# staff, the admin.
VIEWER_BLOCKED_PATHS = ('/settings/', '/payments/settings/', '/billing/', '/admin/', '/staff/', '/messages/settings/',
                        '/messages/whatsapp/', '/messages/sms-settings/', '/verify-phone/', '/welcome/')


class ReadOnlyViewerMiddleware:
    """Someone the landlord let in sees the landlord's account as the landlord does.

    For their requests, request.user becomes the landlord (so every page shows the landlord's records) and
    request.viewer holds their own access. Read-only people can only look; those who can make changes can do
    anything outside VIEWER_BLOCKED_PATHS, and the activity log names them. Their own password and two-step
    pages keep them as themselves.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .models import viewer_for

        access = viewer_for(request.user)
        request.viewer = None
        if access is None or request.path.startswith(VIEWER_OWN_PATHS):
            if access is not None:
                request.viewer = access
            return self.get_response(request)
        if not access.owner.is_active:
            return redirect('logout')
        if request.path.startswith(VIEWER_BLOCKED_PATHS):
            from django.contrib import messages

            messages.info(request, "That page is only for the account owner.")
            return redirect('rentals:dashboard')
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not access.can_edit:
            from django.contrib import messages

            from django.utils.http import url_has_allowed_host_and_scheme

            messages.error(request, "You have read-only access, so you can't make changes.")
            back = request.META.get('HTTP_REFERER', '')
            safe = url_has_allowed_host_and_scheme(back, {request.get_host()}, request.is_secure())
            return redirect(back if safe and back else 'rentals:dashboard')
        from audit.utils import acting_user

        request.viewer, request.real_user, request.user = access, request.user, access.owner
        token = acting_user.set(request.real_user)
        try:
            return self.get_response(request)
        finally:
            acting_user.reset(token)


# --- Invites (Settings → People with access) -----------------------------------------------------------------------

class InviteTokenGenerator(PasswordResetTokenGenerator):
    """One-use link to choose a password: it stops working once they have one (or after PASSWORD_RESET_TIMEOUT)."""

    key_salt = 'accounts.invite'


invite_tokens = InviteTokenGenerator()


def invite_url(access):
    uid = urlsafe_base64_encode(force_bytes(access.user.pk))
    return settings.SITE_URL.rstrip('/') + reverse('accounts:invite', args=[uid, invite_tokens.make_token(access.user)])


def send_invite(access):
    """Email or text the invite link to the person. Returns how it went ('email', 'sms') or None if it couldn't."""
    from notifications.models import SmsMessage
    from notifications.utils import send_sms

    owner = access.owner.get_full_name() or 'A landlord'
    url = invite_url(access)
    access.invited_at = timezone.now()
    access.save(update_fields=['invited_at'])
    if access.user.email:
        try:
            send_mail(
                subject=f'{owner} invited you to their {settings.APP_NAME} account',
                message=(f'Hi {access.user.first_name or "there"},\n\n{owner} has invited you to their rental account on '
                         f'{settings.APP_NAME}, where you can '
                         + ('see and update' if access.can_edit else 'see')
                         + f' their properties, tenants and payments.\n\nChoose a password to get started:\n{url}\n\n'
                         f'The link works for {settings.PASSWORD_RESET_TIMEOUT // 86400} days.\n\n{settings.APP_NAME}'),
                from_email=None, recipient_list=[access.user.email],
            )
            return 'email'
        except Exception:
            logger.exception('Could not email invite for access %s', access.pk)
            return None
    phone = getattr(profile_for(access.user), 'phone', None)
    if phone:
        msg = send_sms(phone, f'{owner} invited you to their {settings.APP_NAME} account. Choose a password: {url}')
        return None if msg.status == SmsMessage.Status.FAILED else 'sms'
    return None


# --- Referrals (Settings → Invite a landlord) ----------------------------------------------------------------------

def referral_code(user):
    """A short code for the landlord's invite link, e.g. "k3-9f2a": the account number in base 36 plus a check."""
    import base64
    import hmac

    ident = base36(user.pk)
    check = hmac.new(settings.SECRET_KEY.encode(), f'referral:{user.pk}'.encode(), hashlib.sha256).digest()
    return f'{ident}-{base64.b32encode(check).decode()[:5].lower()}'


def referrer_from_code(code):
    """The landlord whose invite code this is, or None."""
    import hmac

    ident, _, _check = (code or '').strip().lower().partition('-')
    try:
        pk = int(ident, 36)
    except ValueError:
        return None
    user = get_user_model().objects.filter(pk=pk, is_active=True).first()
    if user is None or not hmac.compare_digest(referral_code(user), code.strip().lower()):
        return None
    return user


def base36(number):
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    out = ''
    while True:
        number, rem = divmod(number, 36)
        out = digits[rem] + out
        if not number:
            return out


def referral_url(user):
    return settings.SITE_URL.rstrip('/') + reverse('accounts:signup') + f'?ref={referral_code(user)}'
