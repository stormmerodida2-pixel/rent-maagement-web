from django.conf import settings
from django.db import models

from rentals.models import Lease


class AuditEntry(models.Model):
    """Who did what to a landlord's records. Append-only."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='audit_entries',
        help_text='The landlord whose records were affected.',
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Who did it. Empty for automatic actions (M-Pesa callbacks, scheduled jobs).',
    )
    lease = models.ForeignKey(Lease, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_entries')
    action = models.CharField(max_length=40)
    summary = models.CharField(max_length=300)
    changes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']
        verbose_name_plural = 'audit entries'

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} {self.action}: {self.summary}'

    @property
    def actor_label(self):
        if self.actor is None:
            return 'System'
        return self.actor.get_full_name() or self.actor.get_username()
