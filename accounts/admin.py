from django.contrib import admin

from .models import SupportSession


@admin.register(SupportSession)
class SupportSessionAdmin(admin.ModelAdmin):
    """Private log of superadmins viewing landlords' accounts. Read only: entries can't be added, edited or deleted."""

    list_display = ['started_at', 'superadmin', 'landlord', 'reason', 'allow_changes', 'ended_at']
    list_filter = ['allow_changes', 'superadmin']
    search_fields = ['landlord__first_name', 'landlord__last_name', 'landlord__email', 'reason']
    date_hierarchy = 'started_at'

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
