from django.urls import path

from . import views

app_name = 'listings'

urlpatterns = [
    path('vacancies/', views.listing_list, name='list'),
    path('vacancies/new/<int:unit_pk>/', views.listing_create, name='create'),
    path('vacancies/<int:pk>/', views.listing_detail, name='detail'),
    path('vacancies/applications/<int:pk>/', views.application_action, name='application_action'),
    path('to-let/<slug:slug>/', views.listing_public, name='public'),
]
