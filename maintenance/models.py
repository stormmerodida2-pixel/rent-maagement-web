import builtins

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from rentals.models import Property, Tenant, Unit


class MaintenanceRequest(models.Model):
    class Priority(models.TextChoices):
        LOW = 'low', _('Low')
        MEDIUM = 'medium', _('Medium')
        HIGH = 'high', _('High')
        URGENT = 'urgent', _('Urgent')

    class Status(models.TextChoices):
        OPEN = 'open', _('Open')
        IN_PROGRESS = 'in_progress', _('In progress')
        RESOLVED = 'resolved', _('Resolved')
        CANCELLED = 'cancelled', _('Cancelled')

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='maintenance_requests')
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, null=True, blank=True, related_name='maintenance_requests',
                             help_text='Leave blank for general maintenance affecting the whole property.')
    reported_by = models.ForeignKey(
        Tenant, on_delete=models.SET_NULL, null=True, blank=True, related_name='maintenance_requests'
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    assigned_to = models.CharField(max_length=200, blank=True, help_text='Contractor or caretaker.')
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} ({self.where})'

    @builtins.property  # the `property` field shadows the builtin
    def is_general(self):
        return self.unit_id is None

    @builtins.property
    def where(self):
        """The house, or "Riverside · General" for the whole property."""
        return str(self.unit) if self.unit_id else f'{self.property.name} · General'

    def save(self, *args, **kwargs):
        if self.unit_id:
            self.property_id = self.unit.property_id
        if self.status == self.Status.RESOLVED and self.resolved_at is None:
            self.resolved_at = timezone.now()
        elif self.status != self.Status.RESOLVED:
            self.resolved_at = None
        super().save(*args, **kwargs)


class RepairRating(models.Model):
    """A tenant's thumbs up or down for a finished repair."""

    repair = models.ForeignKey(MaintenanceRequest, on_delete=models.CASCADE, related_name='ratings')
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='repair_ratings')
    good = models.BooleanField()
    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['repair', 'tenant'], name='one_rating_per_tenant')]
