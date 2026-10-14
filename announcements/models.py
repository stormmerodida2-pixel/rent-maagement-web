import math

from django.conf import settings
from django.db import models
from django.utils import timezone

from rentals.models import Lease, Property, Tenant, Unit


class Announcement(models.Model):
    """A notice from a landlord to some or all of their tenants, by SMS and/or on their portal."""

    class Audience(models.TextChoices):
        ALL = 'all', 'All my tenants'
        PROPERTIES = 'properties', 'Tenants in selected properties'
        UNITS = 'units', 'Tenants in selected units'

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SCHEDULED = 'scheduled', 'Scheduled'
        SENT = 'sent', 'Sent'
        CANCELLED = 'cancelled', 'Cancelled'

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='announcements')
    title = models.CharField(max_length=120)
    body = models.TextField('message', max_length=1000)
    audience = models.CharField(max_length=12, choices=Audience.choices, default=Audience.ALL)
    properties = models.ManyToManyField(Property, blank=True, related_name='announcements')
    units = models.ManyToManyField(Unit, blank=True, related_name='announcements')
    send_sms = models.BooleanField('send by SMS', default=True)
    show_on_portal = models.BooleanField("show on tenants' pages", default=True)
    pinned = models.BooleanField(default=False, help_text="Keep it at the top of tenants' pages.")
    expires_at = models.DateTimeField(
        'hide after', null=True, blank=True,
        help_text="Stops showing on tenants' pages after this (e.g. after the water is back on).",
    )
    scheduled_for = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    # --- Recipients -------------------------------------------------------------------------

    def recipient_leases(self):
        """The landlord's active leases this announcement reaches (one per tenant)."""
        leases = Lease.objects.filter(unit__property__owner=self.owner, is_active=True)
        if self.audience == self.Audience.PROPERTIES:
            leases = leases.filter(unit__property__in=self.properties.all())
        elif self.audience == self.Audience.UNITS:
            leases = leases.filter(unit__in=self.units.all())
        seen, result = set(), []
        for lease in leases.select_related('tenant', 'unit__property').order_by('tenant_id', 'pk'):
            if lease.tenant_id not in seen:  # a tenant renting two units hears it once
                seen.add(lease.tenant_id)
                result.append(lease)
        return result

    # --- SMS ----------------------------------------------------------------------------------

    SMS_LIMIT = 306  # two SMS parts; longer notices point to the portal for the rest

    def sms_text(self):
        text = f'{settings.APP_NAME}: {self.title}. {self.body}'.strip()
        if len(text) <= self.SMS_LIMIT:
            return text
        tail = '... Full notice on your rent page.'
        return text[:self.SMS_LIMIT - len(tail)].rstrip() + tail

    def sms_parts(self):
        text = self.sms_text()
        gsm = all(ord(c) < 128 for c in text)  # emoji/accents switch phones to 70-char parts
        single, multi = (160, 153) if gsm else (70, 67)
        return 1 if len(text) <= single else math.ceil(len(text) / multi)

    def estimated_cost(self, recipients=None):
        count = len(self.recipient_leases()) if recipients is None else recipients
        return round(count * self.sms_parts() * settings.SMS_COST_PER_PART, 2)

    @property
    def is_visible(self):
        return self.status == self.Status.SENT and (self.expires_at is None or self.expires_at > timezone.now())


class Delivery(models.Model):
    """One tenant's copy of an announcement (what their portal shows, and their SMS)."""

    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name='deliveries')
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='announcement_deliveries')
    lease = models.ForeignKey(Lease, on_delete=models.SET_NULL, null=True, related_name='+')
    sms = models.ForeignKey('notifications.SmsMessage', on_delete=models.SET_NULL, null=True, blank=True,
                            related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['announcement', 'tenant'], name='one_delivery_per_tenant')]


class PlatformNotice(models.Model):
    """A message from the people running the site to every landlord (dashboard banner)."""

    class Level(models.TextChoices):
        INFO = 'info', 'Information'
        WARNING = 'warning', 'Warning (e.g. planned maintenance)'

    title = models.CharField(max_length=120)
    body = models.TextField(max_length=600, blank=True)
    link = models.URLField(blank=True, help_text='Optional "Learn more" link.')
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    dismissed_by = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='+')

    class Meta:
        ordering = ['-starts_at']

    def __str__(self):
        return self.title

    @classmethod
    def current_for(cls, user):
        now = timezone.now()
        return (cls.objects.filter(starts_at__lte=now)
                .filter(models.Q(ends_at__isnull=True) | models.Q(ends_at__gt=now))
                .exclude(dismissed_by=user))


def current_notices(tenant, limit=10):
    """Announcements to show on a tenant's page: sent to them, shown on the portal, not expired."""
    now = timezone.now()
    return (Delivery.objects.filter(tenant=tenant, announcement__status=Announcement.Status.SENT,
                                    announcement__show_on_portal=True)
            .filter(models.Q(announcement__expires_at__isnull=True) | models.Q(announcement__expires_at__gt=now))
            .select_related('announcement')
            .order_by('-announcement__pinned', '-announcement__sent_at')[:limit])
