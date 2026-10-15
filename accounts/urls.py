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
