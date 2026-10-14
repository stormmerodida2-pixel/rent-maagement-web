from django.urls import path

from . import views

app_name = 'notifications'

urlpatterns = [
    path('', views.SmsList.as_view(), name='sms_list'),
]
