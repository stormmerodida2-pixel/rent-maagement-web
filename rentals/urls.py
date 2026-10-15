from django.urls import path

from . import crud_views as crud
from . import views

app_name = 'rentals'

urlpatterns = [
    path('', views.home, name='home'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('live/', views.live, name='live'),
    path('leases/<int:pk>/', views.lease_detail, name='lease_detail'),
    path('leases/<int:pk>/remind/', views.send_reminder, name='send_reminder'),
    path('leases/<int:pk>/notice/', views.lease_notice, name='lease_notice'),
    path('leases/<int:pk>/rent/', views.lease_rent_change, name='rent_change'),
    path('leases/<int:pk>/plan/', views.plan_create, name='plan_create'),
    path('plans/', views.plan_list, name='plan_list'),
    path('plans/<int:pk>/cancel/', views.plan_cancel, name='plan_cancel'),
    path('properties/<int:pk>/rent/', views.property_rent_change, name='property_rent_change'),
    path('reminders/', views.remind_all, name='remind_all'),
    path('t/<str:token>/', views.tenant_portal, name='tenant_portal'),

    path('properties/', crud.PropertyList.as_view(), name='property_list'),
    path('properties/new/', crud.PropertyCreate.as_view(), name='property_create'),
    path('properties/import/', crud.property_import, name='property_import'),
    path('properties/import/template.xlsx', crud.import_template_download, name='import_template'),
    path('properties/<int:pk>/', crud.PropertyDetail.as_view(), name='property_detail'),
    path('properties/<int:pk>/edit/', crud.PropertyUpdate.as_view(), name='property_update'),
    path('properties/<int:pk>/rents/', crud.property_rents, name='property_rents'),
    path('properties/<int:pk>/delete/', crud.PropertyDelete.as_view(), name='property_delete'),
    path('properties/<int:property_pk>/units/new/', crud.UnitCreate.as_view(), name='unit_create'),
    path('units/<int:pk>/edit/', crud.UnitUpdate.as_view(), name='unit_update'),
    path('units/<int:pk>/delete/', crud.UnitDelete.as_view(), name='unit_delete'),

    path('tenants/', crud.TenantList.as_view(), name='tenant_list'),
    path('tenants/new/', crud.TenantCreate.as_view(), name='tenant_create'),
    path('tenants/<int:pk>/edit/', crud.TenantUpdate.as_view(), name='tenant_update'),
    path('tenants/<int:pk>/delete/', crud.TenantDelete.as_view(), name='tenant_delete'),

    path('leases/', crud.LeaseList.as_view(), name='lease_list'),
    path('leases/new/', crud.LeaseCreate.as_view(), name='lease_create'),
    path('leases/<int:pk>/edit/', crud.LeaseUpdate.as_view(), name='lease_update'),
    path('leases/<int:pk>/end/', crud.LeaseEnd.as_view(), name='lease_end'),
    path('leases/<int:pk>/charges/new/', crud.ChargeCreate.as_view(), name='charge_create'),
    path('charges/<int:pk>/void/', crud.charge_void, name='charge_void'),
    path('properties/<int:pk>/water/', crud.WaterReadings.as_view(), name='water_readings'),
    path('readings/<int:pk>/void/', crud.reading_void, name='reading_void'),
]
