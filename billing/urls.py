from django.urls import path

from . import views

app_name = 'billing'

urlpatterns = [
    path('billing/', views.billing, name='billing'),
    path('billing/pay/', views.pay, name='pay'),
    path('billing/claim/', views.claim, name='claim'),
    path('billing/invoices/<int:pk>/', views.invoice_detail, name='invoice'),
    path('billing/overview/', views.overview, name='overview'),
    path('billing/overview/invoices/<int:pk>/paid/', views.overview_mark_paid, name='mark_paid'),
    path('billing/overview/invoices/<int:pk>/reject/', views.overview_reject_claim, name='reject_claim'),
    path('billing/overview/landlords/<int:pk>/extend/', views.overview_extend, name='extend'),
    # Safaricom calls this; the token is a secret path segment, like the landlords' webhooks.
    path('billing/hooks/<str:token>/prompt/', views.stk_callback, name='stk_callback'),
]
