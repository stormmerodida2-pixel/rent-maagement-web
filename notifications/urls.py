from django.urls import path

from . import views

app_name = 'notifications'

urlpatterns = [
    path('', views.SmsList.as_view(), name='sms_list'),
    path('settings/', views.preferences, name='preferences'),
    path('whatsapp/', views.whatsapp_settings, name='whatsapp'),
    path('sms-settings/', views.sms_settings, name='sms_settings'),
]
