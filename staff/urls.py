from django.urls import path

from . import views

app_name = 'staff'

urlpatterns = [
    # Staff area
    path('staff/', views.home, name='home'),
    path('staff/leases/<int:pk>/prompt/', views.send_prompt, name='send_prompt'),
    path('staff/leases/<int:pk>/cash/', views.record_cash, name='record_cash'),
    path('staff/prompts/<int:pk>/', views.prompt_status, name='prompt'),
    path('staff/prompts/<int:pk>/check/', views.prompt_check, name='prompt_check'),
    path('staff/repairs/', views.repairs, name='repairs'),
    path('staff/repairs/new/', views.repair_form, name='repair_create'),
    path('staff/repairs/<int:pk>/', views.repair_form, name='repair_update'),
    path('staff/password/', views.StaffPasswordView.as_view(), name='password'),
    # Landlord: Settings → Staff
    path('settings/staff/', views.staff_list, name='manage'),
    path('settings/staff/new/', views.staff_edit, name='add'),
    path('settings/staff/<int:pk>/', views.staff_edit, name='edit'),
]
