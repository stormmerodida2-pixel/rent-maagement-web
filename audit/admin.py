from django.contrib import admin

from .models import AuditEntry


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'owner', 'actor', 'action', 'summary']
    list_filter = ['action']
    search_fields = ['summary', 'owner__username']
    readonly_fields = [f.name for f in AuditEntry._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
