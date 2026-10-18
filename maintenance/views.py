from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST
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
        qs = super().get_queryset().select_related('property', 'unit__property', 'reported_by').annotate(
            thumbs_up=Count('ratings', filter=Q(ratings__good=True)),
            thumbs_down=Count('ratings', filter=Q(ratings__good=False)))
        status = self.request.GET.get('status', 'open')
        if status == 'open':
            return qs.filter(status__in=OPEN_STATUSES).order_by(PRIORITY_ORDER, '-created_at')
        if status in MaintenanceRequest.Status.values:
            qs = qs.filter(status=status)
        return qs.order_by('-created_at')  # explicit: the rating counts make Django drop Meta.ordering

    def get_context_data(self, **kwargs):
        from .models import RepairRating

        # Who does good work: tenants' ratings of finished repairs, by who fixed them.
        by_fixer = (RepairRating.objects.filter(repair__property__owner=self.request.user)
                    .exclude(repair__assigned_to='').values('repair__assigned_to')
                    .annotate(up=Count('pk', filter=Q(good=True)), down=Count('pk', filter=Q(good=False)))
                    .order_by('-up', 'down'))
        return super().get_context_data(status=self.request.GET.get('status', 'open'), fixers=by_fixer, **kwargs)


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

    def get_initial(self):
        """From a unit's or tenant's page: Log repair arrives with the house (and who reported it) filled in."""
        initial = super().get_initial()
        unit, tenant = self.request.GET.get('unit', ''), self.request.GET.get('tenant', '')
        if unit.isdigit():
            initial['where'] = f'u{unit}'
        elif self.request.GET.get('property', '').isdigit():
            initial['where'] = f"p{self.request.GET['property']}"
        if tenant.isdigit():
            initial['reported_by'] = int(tenant)
        return initial


class MaintenanceUpdate(NotifyTenantsMixin, OwnedMixin, FormPageMixin, UpdateView):
    model = MaintenanceRequest
    form_class = MaintenanceForm
    owner_path = 'property__owner'
    audit_action = 'repair.updated'
    page_title = 'Update repair'
    cancel_url = reverse_lazy('maintenance:list')
    success_url = reverse_lazy('maintenance:list')
    success_message = 'Updated "{obj.title}".'


@login_required
@require_POST
def bulk_resolve(request):
    """Mark several repairs done at once from the list, optionally texting the tenants."""
    from audit import utils as audit

    ids = [i for i in request.POST.getlist('repair') if i.isdigit()]
    repairs = list(MaintenanceRequest.objects.filter(pk__in=ids, property__owner=request.user,
                                                     status__in=OPEN_STATUSES).select_related('property', 'unit'))
    if not repairs:
        messages.error(request, 'Tick at least one open repair.')
        return redirect('maintenance:list')
    texted = 0
    for repair in repairs:
        repair.status = MaintenanceRequest.Status.RESOLVED
        repair.save()  # sets resolved_at
        if request.POST.get('notify_tenants'):
            texted += notify_tenants(repair, created=False)
    audit.record(owner=request.user, actor=request.user, action='repair.bulk_resolved',
                 summary=f'Marked {len(repairs)} repair{"s" if len(repairs) != 1 else ""} done: '
                         + ', '.join(r.title for r in repairs)[:200])
    messages.success(request, f'Marked {len(repairs)} repair{"s" if len(repairs) != 1 else ""} done.'
                     + (f' Texted {texted} tenant{"s" if texted != 1 else ""}.' if texted else ''))
    return redirect('maintenance:list')
