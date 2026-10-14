from django import forms
from django.contrib import admin
from django.db import transaction

from rentals.admin import OwnerScopedAdmin

from . import mpesa
from .models import C2BPayment, MpesaAccount, MpesaTransaction, Payment


@admin.register(Payment)
class PaymentAdmin(OwnerScopedAdmin):
    owner_path = 'lease__unit__property__owner'
    list_display = ['paid_at', 'lease', 'amount', 'method', 'reference']
    list_filter = ['method', 'lease__unit__property']
    search_fields = ['reference', 'lease__tenant__full_name']
    date_hierarchy = 'paid_at'


@admin.register(MpesaTransaction)
class MpesaTransactionAdmin(OwnerScopedAdmin):
    owner_path = 'lease__unit__property__owner'
    list_display = ['created_at', 'lease', 'phone', 'amount', 'status', 'receipt_number', 'result_desc']
    list_filter = ['status']
    search_fields = ['checkout_request_id', 'receipt_number', 'phone']
    readonly_fields = [f.name for f in MpesaTransaction._meta.fields]

    def has_add_permission(self, request):
        return False


class MatchedFilter(admin.SimpleListFilter):
    title = 'matched'
    parameter_name = 'matched'

    def lookups(self, request, model_admin):
        return [('no', 'Unmatched'), ('yes', 'Matched')]

    def queryset(self, request, queryset):
        if self.value() == 'no':
            return queryset.filter(payment__isnull=True)
        if self.value() == 'yes':
            return queryset.filter(payment__isnull=False)
        return queryset


class C2BPaymentForm(forms.ModelForm):
    class Meta:
        model = C2BPayment
        fields = ['lease']

    def clean_lease(self):
        lease = self.cleaned_data['lease']
        account = self.instance.account
        if lease and account and lease.unit.property.owner_id != account.owner_id:
            raise forms.ValidationError("This payment went to another landlord's M-Pesa account.")
        return lease


@admin.register(C2BPayment)
class C2BPaymentAdmin(OwnerScopedAdmin):
    # Scoped by the landlord whose Paybill/Till received the money, so they can assign
    # their own unmatched payments.
    owner_path = 'account__owner'
    form = C2BPaymentForm
    list_display = ['trans_time', 'trans_id', 'amount', 'bill_ref', 'payer_name', 'lease', 'matched']
    list_filter = [MatchedFilter]
    search_fields = ['trans_id', 'bill_ref', 'payer_name', 'msisdn']
    date_hierarchy = 'trans_time'

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        fields = [f.name for f in C2BPayment._meta.fields]
        if obj is not None and obj.payment_id is None:
            fields.remove('lease')
        return fields

    @admin.display(boolean=True)
    def matched(self, obj):
        return obj.payment_id is not None

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            if obj.lease_id and obj.payment_id is None:
                mpesa.apply_c2b_payment(obj)
            super().save_model(request, obj, form, change)


@admin.register(MpesaAccount)
class MpesaAccountAdmin(admin.ModelAdmin):
    """Read-only overview for superusers; landlords manage their account in the app."""

    list_display = ['owner', 'payment_type', 'pay_number', 'environment', 'verified_at', 'c2b_registered_at']
    list_filter = ['environment', 'payment_type']
    fields = ['owner', 'environment', 'payment_type', 'shortcode', 'till_number',
              'verified_at', 'c2b_registered_at', 'last_error', 'created_at', 'updated_at']
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser
