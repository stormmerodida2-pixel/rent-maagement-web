from django.db.models import Case, IntegerField, Value, When
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView, UpdateView

from rentals.crud_views import FormPageMixin
from rentals.mixins import OwnedMixin

from .forms import MaintenanceForm
from .models import MaintenanceRequest

OPEN_STATUSES = [MaintenanceRequest.Status.OPEN, MaintenanceRequest.Status.IN_PROGRESS]
PRIORITY_ORDER = Case(
    *[When(priority=p, then=Value(i)) for i, p in enumerate(['urgent', 'high', 'medium', 'low'])],
    output_field=IntegerField(),
)


class MaintenanceList(OwnedMixin, ListView):
    model = MaintenanceRequest
    owner_path = 'unit__property__owner'
    template_name = 'maintenance/maintenance_list.html'
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related('unit__property', 'reported_by')
        status = self.request.GET.get('status', 'open')
        if status == 'open':
            return qs.filter(status__in=OPEN_STATUSES).order_by(PRIORITY_ORDER, '-created_at')
        if status in MaintenanceRequest.Status.values:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        return super().get_context_data(status=self.request.GET.get('status', 'open'), **kwargs)


class MaintenanceCreate(OwnedMixin, FormPageMixin, CreateView):
    model = MaintenanceRequest
    form_class = MaintenanceForm
    owner_path = 'unit__property__owner'
    page_title = 'Log a repair'
    submit_label = 'Log repair'
    cancel_url = reverse_lazy('maintenance:list')
    success_url = reverse_lazy('maintenance:list')
    success_message = 'Logged "{obj.title}".'


class MaintenanceUpdate(OwnedMixin, FormPageMixin, UpdateView):
    model = MaintenanceRequest
    form_class = MaintenanceForm
    owner_path = 'unit__property__owner'
    page_title = 'Update repair'
    cancel_url = reverse_lazy('maintenance:list')
    success_url = reverse_lazy('maintenance:list')
    success_message = 'Updated "{obj.title}".'
