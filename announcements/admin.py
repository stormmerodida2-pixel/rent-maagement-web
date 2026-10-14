from django.contrib import admin

from .models import Announcement, PlatformNotice


@admin.register(PlatformNotice)
class PlatformNoticeAdmin(admin.ModelAdmin):
    """Messages to every landlord, shown as a dismissible banner in their app."""

    list_display = ['title', 'level', 'starts_at', 'ends_at', 'dismissals']
    exclude = ['dismissed_by']

    @admin.display(description='Dismissed by')
    def dismissals(self, obj):
        return obj.dismissed_by.count()

    def has_module_permission(self, request):
        return request.user.is_superuser


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    """Read-only overview for superusers; landlords use the Announcements page."""

    list_display = ['created_at', 'owner', 'title', 'audience', 'status', 'sent_at']
    list_filter = ['status']
    readonly_fields = [f.name for f in Announcement._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_module_permission(self, request):
        return request.user.is_superuser
