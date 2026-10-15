from django.urls import path

from . import views

app_name = 'accounts'

urlpatterns = [
    path('signup/', views.signup, name='signup'),
    path('welcome/', views.welcome, name='welcome'),
    path('settings/', views.settings_page, name='settings'),
    path('settings/profile/', views.profile_edit, name='profile'),
    path('settings/password/', views.ChangePasswordView.as_view(), name='password_change'),
    path('settings/two-step/', views.two_factor_settings, name='two_factor'),
    path('login/verify/', views.login_verify, name='login_verify'),
    path('verify-email/resend/', views.resend_verification, name='resend_verification'),
    path('verify-email/<str:token>/', views.verify_email, name='verify_email'),
]

# Staff (caretaker) pages keep their own 'staff:' namespace.
staff_patterns = ([
    # Staff area
    path('staff/', views.staff_home, name='home'),
    path('staff/leases/<int:pk>/prompt/', views.send_prompt, name='send_prompt'),
    path('staff/leases/<int:pk>/cash/', views.record_cash, name='record_cash'),
    path('staff/prompts/<int:pk>/', views.prompt_status, name='prompt'),
    path('staff/prompts/<int:pk>/check/', views.prompt_check, name='prompt_check'),
    path('staff/repairs/', views.repairs, name='repairs'),
    path('staff/repairs/new/', views.repair_form, name='repair_create'),
    path('staff/repairs/<int:pk>/', views.repair_form, name='repair_update'),
    path('staff/password/', views.StaffPasswordView.as_view(), name='password'),
    path('staff/two-step/', views.two_factor_settings, name='two_factor'),
    # Landlord: Settings → Staff
    path('settings/staff/', views.staff_list, name='manage'),
    path('settings/staff/new/', views.staff_edit, name='add'),
    path('settings/staff/<int:pk>/', views.staff_edit, name='edit'),
], 'staff')
