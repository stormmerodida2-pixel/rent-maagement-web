from django.contrib import messages
from django.db.models import Case, IntegerField, Value, When
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView, UpdateView

from rentals.crud_views import FormPageMixin
from rentals.utils import OwnedMixin

from .forms import MaintenanceForm
from .models import MaintenanceRequest
from .utils import notify_tenants

OPEN_STATUSES = [MaintenanceRequest.Status.OPEN, MaintenanceRequest.Status.IN_PROGRESS]
PRIORITY_ORDER = Case(
    *[When(priority=p, then=Value(i)) for i, p in enumerate(['urgent', 'high', 'medium', 'low'])],
    output_field=IntegerField(),
)


class MaintenanceList(OwnedMixin, ListView):
    model = MaintenanceRequest
    owner_path = 'property__owner'
    template_name = 'maintenance/maintenance_list.html'
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related('property', 'unit__property', 'reported_by')
        status = self.request.GET.get('status', 'open')
        if status == 'open':
            return qs.filter(status__in=OPEN_STATUSES).order_by(PRIORITY_ORDER, '-created_at')
        if status in MaintenanceRequest.Status.values:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        return super().get_context_data(status=self.request.GET.get('status', 'open'), **kwargs)


class NotifyTenantsMixin:
    """After saving, text the tenants if the "Let the tenant know by SMS" box was ticked."""

    def form_valid(self, form):
        created = form.instance.pk is None
        response = super().form_valid(form)
        if form.cleaned_data.get('notify_tenants'):
            sent = notify_tenants(self.object, created)
            messages.info(self.request, f'Texted {sent} tenant{"s" if sent != 1 else ""} about it.' if sent else
                          'No tenant to text: nobody with a phone number lives there, or the repair was cancelled.')
        return response


class MaintenanceCreate(NotifyTenantsMixin, OwnedMixin, FormPageMixin, CreateView):
    model = MaintenanceRequest
    form_class = MaintenanceForm
    owner_path = 'property__owner'
    audit_action = 'repair.logged'
    page_title = 'Log a repair'
    submit_label = 'Log repair'
    cancel_url = reverse_lazy('maintenance:list')
    success_url = reverse_lazy('maintenance:list')
    success_message = 'Logged "{obj.title}".'


class MaintenanceUpdate(NotifyTenantsMixin, OwnedMixin, FormPageMixin, UpdateView):
    model = MaintenanceRequest
    form_class = MaintenanceForm
    owner_path = 'property__owner'
    audit_action = 'repair.updated'
    page_title = 'Update repair'
    cancel_url = reverse_lazy('maintenance:list')
    success_url = reverse_lazy('maintenance:list')
    success_message = 'Updated "{obj.title}".'
