from django.conf import settings
from django.db import models

from payments.utils import EncryptedTextField


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    email_verified_at = models.DateTimeField(null=True, blank=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    terms_version = models.CharField(max_length=20, blank=True)

    def __str__(self):
        return f'Profile of {self.user}'

    @property
    def email_verified(self):
        return self.email_verified_at is not None


def profile_for(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


class TwoFactor(models.Model):
    """Two-step login with an authenticator app (TOTP). Not active until confirmed with a first code."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='two_factor')
    secret = EncryptedTextField()  # base32 TOTP key, encrypted at rest
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_step = models.BigIntegerField(default=0)  # the last 30-second step used, so a code can't be reused
    recovery_codes = models.JSONField(default=list, blank=True)  # SHA-256 hashes of the unused codes
    trust_version = models.PositiveIntegerField(default=1)  # bumping it forgets every "trusted device"
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Two-step login for {self.user}'

    @property
    def enabled(self):
        return self.confirmed_at is not None


def two_factor_enabled(user):
    return bool(user and user.pk) and TwoFactor.objects.filter(user_id=user.pk, confirmed_at__isnull=False).exists()
