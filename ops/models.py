from django.db import models


class JobRun(models.Model):
    """Last run of each scheduled job, so restarts don't skip or repeat daily jobs."""

    name = models.CharField(max_length=60, unique=True)
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_finished_at = models.DateTimeField(null=True, blank=True)
    last_ok = models.BooleanField(default=True)
    last_output = models.TextField(blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name
