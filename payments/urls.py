from django.urls import path

from . import views

app_name = 'payments'

# Each landlord's M-Pesa account has its own secret token in its webhook URLs.
# Safaricom rejects C2B callback URLs containing words like "mpesa" or "safaricom".
urlpatterns = [
    path('hooks/<str:token>/stk/', views.stk_callback, name='stk_callback'),
    path('hooks/<str:token>/c2b/validation/', views.c2b_validation, name='c2b_validation'),
    path('hooks/<str:token>/c2b/confirmation/', views.c2b_confirmation, name='c2b_confirmation'),
    path('leases/<int:lease_id>/request-payment/', views.request_payment, name='request_payment'),
    path('settings/', views.mpesa_settings, name='mpesa_settings'),
    path('statement/', views.statement_upload, name='statement_upload'),
    path('', views.PaymentList.as_view(), name='payment_list'),
    path('new/', views.PaymentCreate.as_view(), name='payment_create'),
    path('<int:pk>/void/', views.payment_void, name='payment_void'),
    path('stk/<int:pk>/check/', views.stk_check, name='stk_check'),
    path('<int:pk>/move/', views.payment_move, name='payment_move'),
    path('unmatched/', views.UnmatchedList.as_view(), name='unmatched_list'),
    path('unmatched/<int:pk>/assign/', views.assign_unmatched, name='assign_unmatched'),
]
