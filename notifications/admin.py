from django.contrib import admin

from rentals.admin import OwnerScopedAdmin

from .models import SmsMessage


@admin.register(SmsMessage)
class SmsMessageAdmin(OwnerScopedAdmin):
    owner_path = 'lease__unit__property__owner'
    list_display = ['created_at', 'phone', 'kind', 'status', 'lease', 'body', 'error']
    list_filter = ['kind', 'status']
    search_fields = ['phone', 'body', 'lease__tenant__full_name']
    readonly_fields = [f.name for f in SmsMessage._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
