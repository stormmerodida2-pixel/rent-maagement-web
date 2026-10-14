from django.views.generic import ListView

from rentals.mixins import OwnedMixin

from .models import AuditEntry

FILTERS = {
    'payments': 'payment.',
    'charges': 'charge.',
    'leases': 'lease.',
    'tenants': 'tenant.',
    'settings': 'mpesa.',
}


class ActivityList(OwnedMixin, ListView):
    model = AuditEntry
    template_name = 'audit/activity.html'
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related('actor', 'lease__tenant')
        prefix = FILTERS.get(self.request.GET.get('type'))
        if prefix:
            qs = qs.filter(action__startswith=prefix)
        if self.request.GET.get('lease', '').isdigit():
            qs = qs.filter(lease_id=int(self.request.GET['lease']))
        return qs

    def get_context_data(self, **kwargs):
        return super().get_context_data(filters=FILTERS, current=self.request.GET.get('type', ''), **kwargs)
