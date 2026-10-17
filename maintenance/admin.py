from django.contrib import admin

from rentals.admin import OwnerScopedAdmin

from .models import MaintenanceRequest


@admin.register(MaintenanceRequest)
class MaintenanceRequestAdmin(OwnerScopedAdmin):
    owner_path = 'property__owner'
    list_display = ['title', 'unit', 'priority', 'status', 'assigned_to', 'created_at', 'resolved_at']
    list_filter = ['status', 'priority', 'property']
    list_editable = ['status']
    search_fields = ['title', 'description', 'unit__label']
    readonly_fields = ['resolved_at']
