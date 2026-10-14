from django.urls import path

from . import views

app_name = 'announcements'

urlpatterns = [
    path('', views.AnnouncementList.as_view(), name='list'),
    path('new/', views.AnnouncementCreate.as_view(), name='create'),
    path('<int:pk>/', views.announcement_detail, name='detail'),
    path('<int:pk>/edit/', views.AnnouncementUpdate.as_view(), name='update'),
    path('notices/<int:pk>/dismiss/', views.dismiss_notice, name='dismiss_notice'),
]
