"""Helpers for accounts: rate limiting and email verification."""

import hashlib

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.mail import send_mail
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .models import profile_for


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
    return user.is_authenticated and profile_for(user).email_verified
