from django.conf import settings
from django.db import models


class DismissedNudge(models.Model):
    """A dashboard suggestion the landlord hid ("Not now"); it can come back after `until`."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    key = models.CharField(max_length=40)
    until = models.DateField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'key'], name='one_dismissal_per_nudge')]
