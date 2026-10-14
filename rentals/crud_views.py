"""In-app pages for landlords to manage properties, units, tenants and leases."""

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import CreateView, DeleteView, DetailView, FormView, ListView, UpdateView

from audit import log as audit
from payments.forms import VoidForm

from .billing import (
    BillingError, format_number, last_reading, record_water_reading, settle_move_out, void_reading,
)
from .forms import ChargeForm, EndLeaseForm, LeaseForm, PropertyForm, TenantForm, UnitForm, WaterReadingsForm
from .mixins import OwnedMixin, SafeDeleteMixin
from .models import Lease, MeterReading, Property, RentCharge, Tenant, Unit


class NextUrlMixin:
    """Honour a safe ?next= so onboarding steps can send the landlord back where they came from."""

    def get_next_url(self):
        nxt = self.request.POST.get('next') or self.request.GET.get('next')
        if nxt and url_has_allowed_host_and_scheme(nxt, {self.request.get_host()}, self.request.is_secure()):
            return nxt
        return None

    def get_context_data(self, **kwargs):
        return super().get_context_data(next=self.get_next_url() or '', **kwargs)


class FormPageMixin(NextUrlMixin):
    """Shared generic form page (templates/form_page.html)."""

    template_name = 'form_page.html'
    page_title = ''
    submit_label = 'Save'
    cancel_url = None
    success_message = 'Saved.'
    # Set to e.g. 'lease.edited' to record the save in the activity log.
    audit_action = None
    audit_summary = None  # format string with {obj}; defaults to success_message

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            page_title=self.page_title, submit_label=self.submit_label,
            cancel_url=self.get_next_url() or self.cancel_url, **kwargs,
        )

    def form_valid(self, form):
        instance = getattr(form, 'instance', None)
        creating = instance is not None and instance.pk is None
        response = super().form_valid(form)
        messages.success(self.request, self.success_message.format(obj=self.object))
        if self.audit_action:
            obj = self.object
            audit.record(
                owner=self.request.user, actor=self.request.user, action=self.audit_action,
                lease=obj if isinstance(obj, Lease) else getattr(obj, 'lease', None),
                summary=(self.audit_summary or self.success_message).format(obj=obj),
                changes={} if creating else audit.form_changes(form),
            )
        nxt = self.get_next_url()
        return redirect(nxt) if nxt else response


# --- Properties & units ---------------------------------------------------------------

class PropertyList(OwnedMixin, ListView):
    model = Property
    template_name = 'rentals/property_list.html'

    def get_queryset(self):
        return super().get_queryset().annotate(
            unit_count=Count('units', distinct=True),
            occupied=Count('units__leases', filter=Q(units__leases__is_active=True), distinct=True),
        ).annotate(vacant=F('unit_count') - F('occupied'))


class PropertyCreate(OwnedMixin, FormPageMixin, CreateView):
    model = Property
    audit_action = 'property.added'
    form_class = PropertyForm
    page_title = 'Add a property'
    submit_label = 'Add property'
    cancel_url = reverse_lazy('rentals:property_list')
    success_message = 'Added {obj}.'

    def get_success_url(self):
        return reverse('rentals:property_detail', args=[self.object.pk])


class PropertyUpdate(OwnedMixin, FormPageMixin, UpdateView):
    model = Property
    audit_action = 'property.edited'
    audit_summary = 'Edited property {obj}.'
    form_class = PropertyForm
    page_title = 'Edit property'

    def get_success_url(self):
        return reverse('rentals:property_detail', args=[self.object.pk])

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['cancel_url'] = ctx['cancel_url'] or self.get_success_url()
        ctx['delete_url'] = reverse('rentals:property_delete', args=[self.object.pk])
        return ctx


class PropertyDelete(OwnedMixin, SafeDeleteMixin, DeleteView):
    model = Property
    template_name = 'confirm_delete.html'
    form_takes_owner = False
    success_url = reverse_lazy('rentals:property_list')
    protected_message = 'This property has units with leases. End and remove those leases first.'


class PropertyDetail(OwnedMixin, DetailView):
    model = Property
    template_name = 'rentals/property_detail.html'

    def get_context_data(self, **kwargs):
        units = self.object.units.prefetch_related('leases__tenant')
        rows = [(unit, next((l for l in unit.leases.all() if l.is_active), None)) for unit in units]
        return super().get_context_data(
            unit_rows=rows, unit_form=UnitForm(property=self.object), **kwargs,
        )


class UnitCreate(OwnedMixin, FormPageMixin, CreateView):
    """Add a unit to one of the landlord's properties (also posted to from the property page)."""

    model = Unit
    audit_action = 'unit.added'
    form_class = UnitForm
    form_takes_owner = False
    page_title = 'Add a unit'
    success_message = 'Added unit {obj.label}.'

    def dispatch(self, request, *args, **kwargs):
        self.property = get_object_or_404(Property, pk=kwargs['property_pk'], owner=request.user) \
            if request.user.is_authenticated else None
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['property'] = self.property
        return kwargs

    def get_success_url(self):
        return reverse('rentals:property_detail', args=[self.property.pk])

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['cancel_url'] = ctx['cancel_url'] or self.get_success_url()
        return ctx


class UnitUpdate(OwnedMixin, FormPageMixin, UpdateView):
    model = Unit
    audit_action = 'unit.edited'
    audit_summary = 'Edited unit {obj}.'
    form_class = UnitForm
    form_takes_owner = False
    owner_path = 'property__owner'
    page_title = 'Edit unit'

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['property'] = self.object.property
        return kwargs

    def get_success_url(self):
        return reverse('rentals:property_detail', args=[self.object.property_id])

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['cancel_url'] = ctx['cancel_url'] or self.get_success_url()
        ctx['delete_url'] = reverse('rentals:unit_delete', args=[self.object.pk])
        return ctx


class UnitDelete(OwnedMixin, SafeDeleteMixin, DeleteView):
    model = Unit
    owner_path = 'property__owner'
    template_name = 'confirm_delete.html'
    form_takes_owner = False
    protected_message = 'This unit has lease history, so it can\'t be deleted.'

    def get_success_url(self):
        return reverse('rentals:property_detail', args=[self.object.property_id])


# --- Tenants ----------------------------------------------------------------------------

class TenantList(OwnedMixin, ListView):
    model = Tenant
    template_name = 'rentals/tenant_list.html'
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().prefetch_related('leases__unit__property')
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(full_name__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['q'] = self.request.GET.get('q', '')
        ctx['rows'] = [
            (t, next((l for l in t.leases.all() if l.is_active), None)) for t in ctx['object_list']
        ]
        return ctx


class TenantCreate(OwnedMixin, FormPageMixin, CreateView):
    model = Tenant
    audit_action = 'tenant.added'
    audit_summary = 'Added tenant {obj}.'
    form_class = TenantForm
    page_title = 'Add a tenant'
    submit_label = 'Add tenant'
    cancel_url = reverse_lazy('rentals:tenant_list')
    success_message = 'Added {obj}. Now start a lease for them.'

    def get_success_url(self):
        return reverse('rentals:lease_create') + f'?tenant={self.object.pk}'


class TenantUpdate(OwnedMixin, FormPageMixin, UpdateView):
    model = Tenant
    audit_action = 'tenant.edited'
    audit_summary = 'Edited tenant {obj}.'
    form_class = TenantForm
    page_title = 'Edit tenant'
    cancel_url = reverse_lazy('rentals:tenant_list')
    success_url = reverse_lazy('rentals:tenant_list')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['delete_url'] = reverse('rentals:tenant_delete', args=[self.object.pk])
        return ctx


class TenantDelete(OwnedMixin, SafeDeleteMixin, DeleteView):
    model = Tenant
    template_name = 'confirm_delete.html'
    form_takes_owner = False
    success_url = reverse_lazy('rentals:tenant_list')
    protected_message = 'This tenant has lease history, so they can\'t be deleted.'


# --- Leases -----------------------------------------------------------------------------

class LeaseList(OwnedMixin, ListView):
    model = Lease
    owner_path = 'unit__property__owner'
    template_name = 'rentals/lease_list.html'
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related('tenant', 'unit__property')
        status = self.request.GET.get('status', 'active')
        if status == 'active':
            qs = qs.filter(is_active=True)
        elif status == 'ended':
            qs = qs.filter(is_active=False)
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(tenant__full_name__icontains=q) | Q(unit__label__icontains=q)
                           | Q(unit__property__name__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        rows = [(lease, lease.balance) for lease in ctx['object_list']]
        if self.request.GET.get('owing'):
            rows = [r for r in rows if r[1] > 0]
        ctx.update(rows=rows, status=self.request.GET.get('status', 'active'),
                   q=self.request.GET.get('q', ''), owing=bool(self.request.GET.get('owing')))
        return ctx


class LeaseCreate(OwnedMixin, FormPageMixin, CreateView):
    model = Lease
    audit_action = 'lease.started'
    form_class = LeaseForm
    owner_path = 'unit__property__owner'
    page_title = 'Start a lease'
    submit_label = 'Start lease'
    cancel_url = reverse_lazy('rentals:lease_list')
    success_message = 'Lease started for {obj.tenant} in {obj.unit}.'

    def get_initial(self):
        initial = super().get_initial()
        for name in ('tenant', 'unit'):
            if self.request.GET.get(name):
                initial[name] = self.request.GET[name]
        return initial

    def get_success_url(self):
        return reverse('rentals:lease_detail', args=[self.object.pk])


class LeaseUpdate(OwnedMixin, FormPageMixin, UpdateView):
    model = Lease
    audit_action = 'lease.edited'
    audit_summary = 'Edited lease {obj}.'
    form_class = LeaseForm
    owner_path = 'unit__property__owner'
    page_title = 'Edit lease'

    def get_success_url(self):
        return reverse('rentals:lease_detail', args=[self.object.pk])

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['cancel_url'] = ctx['cancel_url'] or self.get_success_url()
        return ctx


class LeaseEnd(OwnedMixin, FormPageMixin, FormView):
    """Move-out: set the end date and free the unit. Charges and payments are kept."""

    form_class = EndLeaseForm
    form_takes_owner = False
    page_title = 'End lease'
    submit_label = 'End lease'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.lease = get_object_or_404(
            Lease, pk=kwargs['pk'], unit__property__owner=request.user, is_active=True,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['lease'] = self.lease
        return kwargs

    def get_initial(self):
        return {**super().get_initial(), 'apply_deposit': self.lease.deposit_held > 0}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['cancel_url'] = reverse('rentals:lease_detail', args=[self.lease.pk])
        balance, held = self.lease.balance, self.lease.deposit_held
        parts = [f'{self.lease.tenant} moves out of {self.lease.unit} and the unit becomes vacant.']
        parts.append(f'They currently owe KES {balance:,.0f}.' if balance > 0 else
                     f'They are KES {-balance:,.0f} in credit.' if balance < 0 else 'Their account is clear.')
        if held > 0:
            parts.append(f'Deposit held: KES {held:,.0f} (counted as paid if they paid what was billed).')
        ctx['intro'] = ' '.join(parts)
        return ctx

    def form_valid(self, form):
        data = form.cleaned_data
        final = settle_move_out(
            self.lease, data['end_date'], deductions=data.get('deductions') or Decimal('0'),
            reason=data.get('reason', ''), apply_deposit=data['apply_deposit'], refund_paid=data['refund_paid'],
        )
        audit.record(
            owner=self.request.user, actor=self.request.user, lease=self.lease, action='lease.ended',
            summary=f'Ended lease {self.lease} on {data["end_date"]:%d %b %Y}; final balance KES {final:,.2f}.',
            changes={'Deductions': (None, data.get('deductions')), 'Reason': (None, data.get('reason') or None),
                     'Deposit applied': (None, data['apply_deposit']), 'Refund paid': (None, data['refund_paid'])},
        )
        if final > 0:
            outcome = f'They still owe KES {final:,.0f}.'
        elif final < 0:
            outcome = (f'Refund due to the tenant: KES {-final:,.0f}. When you pay it, '
                       'add it with "Add charge" as "Refund paid to tenant".')
        else:
            outcome = 'Their account is settled.'
        messages.success(self.request, f'Lease ended. {self.lease.unit} is now vacant. {outcome}')
        return redirect('rentals:lease_detail', pk=self.lease.pk)


class ChargeCreate(OwnedMixin, FormPageMixin, CreateView):
    """A one-off charge or credit on a lease, e.g. a repair the tenant caused."""

    model = RentCharge
    audit_action = 'charge.added'
    audit_summary = 'Added charge "{obj.label}" of KES {obj.amount:,.2f} to {obj.lease}.'
    form_class = ChargeForm
    form_takes_owner = False
    page_title = 'Add a charge'
    submit_label = 'Add charge'
    success_message = 'Added "{obj.label}".'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.lease = get_object_or_404(Lease, pk=kwargs['pk'], unit__property__owner=request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), 'lease': self.lease}

    def get_success_url(self):
        return reverse('rentals:lease_detail', args=[self.lease.pk])

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['cancel_url'] = self.get_success_url()
        ctx['intro'] = f'Billed to {self.lease.tenant} ({self.lease.unit}). Use a negative amount for a credit.'
        return ctx


class WaterReadings(OwnedMixin, FormPageMixin, FormView):
    """Enter this month's meter readings for every unit in a property at once."""

    form_class = WaterReadingsForm
    form_takes_owner = False
    template_name = 'rentals/water_readings.html'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.property = get_object_or_404(Property, pk=kwargs['pk'], owner=request.user)
        self.units = list(self.property.units.all())
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), 'units': self.units}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form = ctx['form']
        ctx.update(
            property=self.property,
            rows=[(unit, last_reading(unit), form[f'unit_{unit.pk}'],
                   next((l for l in unit.leases.all() if l.is_active), None)) for unit in self.units],
            cancel_url=reverse('rentals:property_detail', args=[self.property.pk]),
        )
        return ctx

    def form_valid(self, form):
        billed, errors = Decimal('0'), []
        count = 0
        for unit, value in form.readings():
            try:
                reading = record_water_reading(unit, form.cleaned_data['reading_date'], value)
            except BillingError as exc:
                errors.append(str(exc))
                continue
            count += 1
            if reading.charge:
                billed += reading.charge.amount
        for error in errors:
            messages.error(self.request, error)
        if count:
            audit.record(
                owner=self.request.user, actor=self.request.user, action='water.billed',
                summary=f'Saved {count} water reading(s) for {self.property}; billed KES {billed:,.2f}.',
            )
            messages.success(self.request, f'Saved {count} reading(s); billed KES {billed:,.0f} for water.')
        return redirect('rentals:property_detail', pk=self.property.pk)


@login_required
def charge_void(request, pk):
    """Void a charge billed by mistake (wrong water reading, fee waived…). Kept in history."""
    charge = get_object_or_404(
        RentCharge.objects.select_related('lease__tenant'), pk=pk, lease__unit__property__owner=request.user,
    )
    back = reverse('rentals:lease_detail', args=[charge.lease_id])
    form = VoidForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        charge.void(request.user, form.cleaned_data['reason'])
        audit.record(
            owner=request.user, actor=request.user, lease=charge.lease, action='charge.voided',
            summary=f'Voided "{charge.label}" (KES {charge.amount:,.2f}) for {charge.lease.tenant}: '
                    f'{form.cleaned_data["reason"]}',
        )
        messages.success(request, f'Voided "{charge.label}". Their balance has been updated.')
        return redirect(back)
    note = ''
    if charge.kind in RentCharge.MONTHLY_KINDS:
        note = ' It won\'t be billed again for this month. Use "Add charge" if you need to bill a corrected amount.'
    return render(request, 'form_page.html', {
        'form': form, 'page_title': 'Void charge', 'submit_label': 'Void charge', 'cancel_url': back,
        'intro': f'"{charge.label}", KES {charge.amount:,.2f}, for {charge.lease.tenant}. '
                 f'It will no longer count towards their balance.{note}',
    })


@login_required
def reading_void(request, pk):
    """Void a mistyped meter reading (and its water charge) so the correct one can be entered."""
    reading = get_object_or_404(
        MeterReading.objects.select_related('unit__property'), pk=pk, unit__property__owner=request.user,
    )
    back = reverse('rentals:water_readings', args=[reading.unit.property_id])
    form = VoidForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            void_reading(reading, request.user, form.cleaned_data['reason'])
        except BillingError as exc:
            messages.error(request, str(exc))
            return redirect(back)
        charge = RentCharge.all_objects.filter(pk=reading.charge_id).select_related('lease').first()
        audit.record(
            owner=request.user, actor=request.user, lease=charge.lease if charge else None, action='reading.voided',
            summary=f'Voided water reading {format_number(reading.value)} for {reading.unit} ({reading.reading_date:%d %b %Y})'
                    + (f' and its charge of KES {charge.amount:,.2f}' if charge else '')
                    + f': {form.cleaned_data["reason"]}',
        )
        messages.success(request, f'Voided the reading for {reading.unit.label}. You can now enter the correct one.')
        return redirect(back)
    charge = RentCharge.objects.filter(pk=reading.charge_id).first()
    return render(request, 'form_page.html', {
        'form': form, 'page_title': 'Void meter reading', 'submit_label': 'Void reading', 'cancel_url': back,
        'intro': f'Reading {format_number(reading.value)} for {reading.unit} on {reading.reading_date:%d %b %Y}.'
                 + (f' Its water charge of KES {charge.amount:,.2f} will be voided too.' if charge else '')
                 + ' The previous reading becomes the starting point again.',
    })
