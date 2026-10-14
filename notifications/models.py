from django.conf import settings
from django.db import models

from rentals.models import Lease


class SmsMessage(models.Model):
    class Kind(models.TextChoices):
        REMINDER = 'reminder', 'Rent reminder'
        RECEIPT = 'receipt', 'Payment receipt'
        ANNOUNCEMENT = 'announcement', 'Announcement'
        REPORT = 'report', 'Monthly report'
        OTHER = 'other', 'Other'

    class Status(models.TextChoices):
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'
        LOGGED = 'logged', 'Logged only (console backend)'

    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, null=True, blank=True, related_name='sms_messages')
    phone = models.CharField(max_length=20)
    body = models.TextField()
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.OTHER)
    status = models.CharField(max_length=10, choices=Status.choices)
    provider_message_id = models.CharField(max_length=100, blank=True)
    cost = models.CharField(max_length=30, blank=True)
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'SMS message'

    def __str__(self):
        return f'{self.get_kind_display()} to {self.phone} ({self.status})'


class LandlordPreferences(models.Model):
    """How a landlord wants to hear from us (monthly report card)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='preferences')
    phone = models.CharField('your phone number', max_length=20, blank=True,
                             help_text='For SMS from us, e.g. 0712 345 678.')
    report_email = models.BooleanField('email me a monthly report card', default=True)
    report_sms = models.BooleanField('SMS me a short monthly summary', default=False)

    def __str__(self):
        return f'Preferences of {self.user}'

    @classmethod
    def for_user(cls, user):
        return cls.objects.get_or_create(user=user)[0]


class ReportCardSent(models.Model):
    """One per landlord per month, so the report card is never sent twice."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    month = models.DateField()
    sent_at = models.DateTimeField(auto_now_add=True)
    email_sent = models.BooleanField(default=False)
    sms_sent = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'month'], name='one_report_card_per_month')]
