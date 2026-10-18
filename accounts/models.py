from django.conf import settings
from django.db import models

from payments.utils import EncryptedTextField


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    # Log in with this or the email; stored as 2547XXXXXXXX. Empty (not '') when unset, so it can be unique.
    phone = models.CharField(max_length=12, unique=True, null=True, blank=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    terms_version = models.CharField(max_length=20, blank=True)

    def __str__(self):
        return f'Profile of {self.user}'

    @property
    def email_verified(self):
        return self.email_verified_at is not None

    @property
    def phone_verified(self):
        return bool(self.phone) and self.phone_verified_at is not None


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



class StaffMember(models.Model):
    """A caretaker or agent who works for a landlord: can prompt tenants to pay, record cash and log
    repairs for the landlord's properties (or only some of them), but never sees totals or settings."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='staff_members')
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='staff_membership')
    phone = models.CharField(max_length=20, blank=True)
    properties = models.ManyToManyField(
        'rentals.Property', blank=True, related_name='staff_members',
        help_text='Leave all unticked to cover every property, including ones added later.',
    )
    can_read_meters = models.BooleanField('can enter water meter readings', default=True)
    can_record_spending = models.BooleanField('can record spending from cash they hold', default=True,
                                              help_text='You approve each one before it counts.')
    can_add_notes = models.BooleanField('can add notes on tenants', default=True,
                                        help_text='e.g. "Promised to pay KES 10,000 by Friday".')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['user__first_name', 'user__username']

    def __str__(self):
        return self.name

    @property
    def name(self):
        return self.user.get_full_name() or self.user.get_username()

    def property_ids(self):
        return list(self.properties.values_list('pk', flat=True))

    def leases(self):
        """Active leases this person looks after."""
        from rentals.models import Lease  # rentals imports accounts helpers; keep this module importable first

        qs = Lease.objects.filter(unit__property__owner=self.owner, is_active=True)
        ids = self.property_ids()
        return qs.filter(unit__property__in=ids) if ids else qs

    def looked_after(self):
        """The properties this person looks after."""
        from rentals.models import Property

        qs = Property.objects.filter(owner=self.owner)
        ids = self.property_ids()
        return qs.filter(pk__in=ids) if ids else qs

    def units(self):
        from rentals.models import Unit

        qs = Unit.objects.filter(property__owner=self.owner)
        ids = self.property_ids()
        return qs.filter(property__in=ids) if ids else qs


def staff_for(user):
    """The StaffMember for a signed-in user, or None for landlords and visitors."""
    if not user.is_authenticated:
        return None
    try:
        return user.staff_membership
    except StaffMember.DoesNotExist:
        return None


class ViewerAccess(models.Model):
    """Someone the landlord lets into their account: a spouse or co-owner, an accountant. They log in with their
    own email or phone and password and see the landlord's account; with `can_edit` they can also make changes
    (record payments, tenants, repairs), but never the landlord's settings, M-Pesa, billing or staff."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='viewers')
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='viewer_access')
    role = models.CharField('who they are', max_length=60, blank=True,
                            help_text='e.g. "Co-owner", "Accountant". Shown on your list only.')
    can_edit = models.BooleanField('can make changes', default=False)
    invited_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['user__first_name']

    def __str__(self):
        return f'{self.user.get_full_name() or self.user} ({self.level.lower()} for {self.owner})'

    @property
    def level(self):
        return 'Can make changes' if self.can_edit else 'Read-only'

    @property
    def joined(self):
        """They've chosen a password (or the landlord set one): the invite has been used."""
        return self.user.has_usable_password()

    @property
    def name(self):
        return self.user.get_full_name() or self.user.get_username()


def viewer_for(user):
    """The ViewerAccess for a signed-in user, or None for everyone else."""
    if not user.is_authenticated:
        return None
    try:
        return user.viewer_access
    except ViewerAccess.DoesNotExist:
        return None


class Referral(models.Model):
    """A landlord who signed up through another landlord's invite link. The referrer earns a free month when the
    new landlord pays their first bill."""

    referrer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='referrals')
    referred = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='referred_by')
    created_at = models.DateTimeField(auto_now_add=True)
    rewarded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
