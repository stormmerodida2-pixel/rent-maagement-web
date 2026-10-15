from django.contrib import admin

from .models import Application, Listing


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = ['title', 'unit', 'rent', 'is_open', 'views', 'created_at']
    list_filter = ['is_open']


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ['name', 'phone', 'listing', 'status', 'created_at']
    list_filter = ['status']
