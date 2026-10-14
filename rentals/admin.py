import datetime

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html

from .models import Lease, Property, RentCharge, Tenant, Unit

# How to reach the owning landlord from each model, used to scope querysets.
OWNER_PATHS = {
    Property: 'owner',
    Tenant: 'owner',
    Unit: 'property__owner',
    Lease: 'unit__property__owner',
    RentCharge: 'lease__unit__property__owner',
}


def owned(queryset, user):
    """Restrict a queryset to records belonging to `user` (superusers see everything)."""
    if user.is_superuser:
        return queryset
    path = OWNER_PATHS.get(queryset.model)
    if path is None:
        return queryset
    return queryset.filter(**{path: user})


class OwnerScopedAdmin(admin.ModelAdmin):
    """Landlords only see and pick from their own properties, tenants, units and leases."""

    owner_path = None

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser or not self.owner_path:
            return qs
        return qs.filter(**{self.owner_path: request.user})

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        related = db_field.related_model
        if related in OWNER_PATHS:
            kwargs['queryset'] = owned(related._default_manager.all(), request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_exclude(self, request, obj=None):
        exclude = list(super().get_exclude(request, obj) or [])
        if self.owner_path == 'owner' and not request.user.is_superuser:
            exclude.append('owner')
        return exclude

    def save_model(self, request, obj, form, change):
        if self.owner_path == 'owner' and not obj.owner_id:
            obj.owner = request.user
        super().save_model(request, obj, form, change)


class UnitInline(admin.TabularInline):
    model = Unit
    extra = 1


@admin.register(Property)
class PropertyAdmin(OwnerScopedAdmin):
    owner_path = 'owner'
    list_display = ['name', 'address', 'unit_count']
    search_fields = ['name', 'address']
    inlines = [UnitInline]

    @admin.display(description='Units')
    def unit_count(self, obj):
        return obj.units.count()


@admin.register(Unit)
class UnitAdmin(OwnerScopedAdmin):
    owner_path = 'property__owner'
    list_display = ['label', 'property', 'bedrooms', 'default_rent', 'current_tenant']
    list_filter = ['property']
    search_fields = ['label', 'property__name']

    @admin.display(description='Tenant')
    def current_tenant(self, obj):
        lease = obj.active_lease()
        return lease.tenant if lease else format_html('<em>vacant</em>')


@admin.register(Tenant)
class TenantAdmin(OwnerScopedAdmin):
    owner_path = 'owner'
    list_display = ['full_name', 'phone', 'email']
    search_fields = ['full_name', 'phone', 'email']  # id_number is encrypted, so not searchable
    readonly_fields = ['portal_link']
    actions = ['reset_portal_links']

    @admin.display(description='Self-service link')
    def portal_link(self, obj):
        if not obj.pk:
            return 'Available after saving.'
        url = obj.portal_url()
        return format_html('<a href="{}" target="_blank">{}</a>', url, url)

    @admin.action(description='Reset self-service link (old link stops working)')
    def reset_portal_links(self, request, queryset):
        for tenant in queryset:
            tenant.reset_portal_token()
        self.message_user(request, f'Reset {queryset.count()} link(s).', messages.SUCCESS)


class RentChargeInline(admin.TabularInline):
    model = RentCharge
    extra = 0
    fields = ['period', 'amount', 'due_date']


@admin.register(Lease)
class LeaseAdmin(OwnerScopedAdmin):
    owner_path = 'unit__property__owner'
    list_display = ['tenant', 'unit', 'monthly_rent', 'start_date', 'is_active', 'balance_display', 'open_link']
    list_filter = ['is_active', 'unit__property']
    search_fields = ['tenant__full_name', 'unit__label']
    inlines = [RentChargeInline]
    actions = ['bill_current_month', 'send_reminders']

    @admin.display(description='Balance (KES)')
    def balance_display(self, obj):
        return f'{obj.balance:,.0f}'

    @admin.display(description='')
    def open_link(self, obj):
        return format_html('<a href="{}">Open →</a>', reverse('rentals:lease_detail', args=[obj.pk]))

    @admin.action(description='Bill rent for the current month')
    def bill_current_month(self, request, queryset):
        today = datetime.date.today()
        created = sum(lease.ensure_charge_for(today)[1] for lease in queryset.filter(is_active=True))
        self.message_user(request, f'Created {created} rent charge(s).', messages.SUCCESS)

    @admin.action(description='Send SMS reminder to tenants with a balance')
    def send_reminders(self, request, queryset):
        from notifications.utils import send_rent_reminder

        sent = failed = 0
        for lease in queryset.select_related('tenant', 'unit'):
            balance = lease.balance
            if balance > 0:
                msg = send_rent_reminder(lease, balance)
                failed += msg.status == msg.Status.FAILED
                sent += msg.status != msg.Status.FAILED
        level = messages.WARNING if failed else messages.SUCCESS
        self.message_user(request, f'Sent {sent} reminder(s), {failed} failed.', level)


@admin.register(RentCharge)
class RentChargeAdmin(OwnerScopedAdmin):
    owner_path = 'lease__unit__property__owner'
    list_display = ['lease', 'period', 'amount', 'due_date']
    list_filter = ['period', 'lease__unit__property']
    date_hierarchy = 'period'
