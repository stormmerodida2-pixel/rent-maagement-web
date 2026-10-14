from django.urls import path

from . import views

app_name = 'maintenance'

urlpatterns = [
    path('', views.MaintenanceList.as_view(), name='list'),
    path('new/', views.MaintenanceCreate.as_view(), name='create'),
    path('<int:pk>/', views.MaintenanceUpdate.as_view(), name='update'),
]
