from django.contrib import admin

from .models import Complaint, ComplaintMessage, SupportMessage


@admin.register(SupportMessage)
class SupportMessageAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'name', 'email', 'topic', 'handled', 'short_message']
    list_filter = ['handled', 'topic']
    list_editable = ['handled']
    search_fields = ['name', 'email', 'message']
    readonly_fields = ['name', 'email', 'user', 'topic', 'message', 'page', 'created_at']

    @admin.display(description='Message')
    def short_message(self, obj):
        return obj.message[:80]

    def has_add_permission(self, request):
        return False


class ComplaintMessageInline(admin.TabularInline):
    model = ComplaintMessage
    extra = 0
    readonly_fields = ['author', 'body', 'created_at']
    can_delete = False


@admin.register(Complaint)
class ComplaintAdmin(admin.ModelAdmin):
    """Superuser overview only; landlords handle complaints in the app."""

    list_display = ['updated_at', 'owner', 'tenant', 'category', 'subject', 'status']
    list_filter = ['status', 'category']
    inlines = [ComplaintMessageInline]
    readonly_fields = ['owner', 'tenant', 'lease', 'category', 'subject', 'created_at', 'resolved_at']

    def has_module_permission(self, request):
        return request.user.is_superuser
