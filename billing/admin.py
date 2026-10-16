from django.contrib import admin

from .models import BillingAccount, Invoice


class SuperuserOnly(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(BillingAccount)
class BillingAccountAdmin(SuperuserOnly):
    list_display = ['owner', 'trial_ends', 'paid_until']
    search_fields = ['owner__username', 'owner__email']


@admin.register(Invoice)
class InvoiceAdmin(SuperuserOnly):
    list_display = ['number', 'account', 'period_start', 'amount', 'status', 'method', 'reference']
    list_filter = ['status', 'method']
    search_fields = ['account__owner__username', 'reference', 'claimed_reference']
