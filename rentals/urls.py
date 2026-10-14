from django.urls import path

from . import crud_views as crud
from . import views

app_name = 'rentals'

urlpatterns = [
    path('', views.home, name='home'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('leases/<int:pk>/', views.lease_detail, name='lease_detail'),
    path('leases/<int:pk>/remind/', views.send_reminder, name='send_reminder'),
    path('t/<str:token>/', views.tenant_portal, name='tenant_portal'),

    path('properties/', crud.PropertyList.as_view(), name='property_list'),
    path('properties/new/', crud.PropertyCreate.as_view(), name='property_create'),
    path('properties/<int:pk>/', crud.PropertyDetail.as_view(), name='property_detail'),
    path('properties/<int:pk>/edit/', crud.PropertyUpdate.as_view(), name='property_update'),
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
    path('properties/<int:pk>/water/', crud.WaterReadings.as_view(), name='water_readings'),
]
