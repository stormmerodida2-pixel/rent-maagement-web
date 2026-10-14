"""Email verification links: signed, time-limited, and invalidated if the email changes."""

from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .models import profile_for

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
