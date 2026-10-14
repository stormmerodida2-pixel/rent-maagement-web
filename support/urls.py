from django.urls import path

from . import views

app_name = 'support'

urlpatterns = [
    path('complaints/', views.ComplaintList.as_view(), name='complaint_list'),
    path('complaints/<int:pk>/', views.complaint_detail, name='complaint_detail'),
    path('contact/', views.contact, name='contact'),
    path('t/<str:token>/messages/', views.tenant_messages, name='tenant_messages'),
    path('t/<str:token>/messages/<int:pk>/', views.tenant_complaint, name='tenant_complaint'),
]
