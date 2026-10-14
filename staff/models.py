from django.conf import settings
from django.db import models

from rentals.models import Lease, Property, Unit


class StaffMember(models.Model):
    """A caretaker or agent who works for a landlord: can prompt tenants to pay, record cash and log
    repairs for the landlord's properties (or only some of them), but never sees totals or settings."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='staff_members')
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='staff_membership')
    phone = models.CharField(max_length=20, blank=True)
    properties = models.ManyToManyField(
        Property, blank=True, related_name='staff_members',
        help_text='Leave all unticked to cover every property, including ones added later.',
    )
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
        qs = Lease.objects.filter(unit__property__owner=self.owner, is_active=True)
        ids = self.property_ids()
        return qs.filter(unit__property__in=ids) if ids else qs

    def units(self):
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
