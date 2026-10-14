from django.urls import path

from . import views

app_name = 'reports'

urlpatterns = [
    path('', views.overview, name='overview'),
    path('rent-roll.csv', views.rent_roll_csv, name='rent_roll_csv'),
    path('payments.csv', views.payments_csv, name='payments_csv'),
    path('leases/<int:pk>/statement/', views.lease_statement, name='lease_statement'),
    path('payments/<int:pk>/receipt/', views.payment_receipt, name='payment_receipt'),
    path('t/<str:token>/leases/<int:lease_pk>/statement/', views.tenant_statement, name='tenant_statement'),
    path('t/<str:token>/receipts/<int:payment_pk>/', views.tenant_receipt, name='tenant_receipt'),
]
