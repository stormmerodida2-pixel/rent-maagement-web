from django.db import models

from rentals.models import Lease


class SmsMessage(models.Model):
    class Kind(models.TextChoices):
        REMINDER = 'reminder', 'Rent reminder'
        RECEIPT = 'receipt', 'Payment receipt'
        ANNOUNCEMENT = 'announcement', 'Announcement'
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
