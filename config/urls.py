from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.auth.views import redirect_to_login
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path, reverse

from accounts import impersonation
from accounts import views as account_views
from accounts.urls import staff_patterns
from ops.views import business_details, healthz
from reports import views as report_views

from . import views
from .views import SITEMAPS, robots_txt

admin.site.site_header = settings.APP_NAME
admin.site.site_title = settings.APP_NAME
admin.site.index_title = 'Manage your rentals'
# The admin's own login page gets the same password-guessing limits as the app's.
# The admin logs in through the app's own login page, so it gets the same throttling and two-step check.
admin.site.login = lambda request, extra_context=None: redirect_to_login(
    request.GET.get('next') or reverse('admin:index'), login_url=reverse('login'))

urlpatterns = [
    path('healthz/', healthz, name='healthz'),
    path('robots.txt', robots_txt, name='robots_txt'),
    path('manifest.webmanifest', views.manifest, name='manifest'),
    path('t/<str:token>/manifest.webmanifest', views.tenant_manifest, name='tenant_manifest'),
    path('sw.js', views.service_worker, name='service_worker'),
    path('offline/', views.offline, name='offline'),
    path('sitemap.xml', sitemap, {'sitemaps': SITEMAPS}, name='sitemap'),
    path('privacy/', views.privacy, name='privacy'),
    path('terms/', views.terms, name='terms'),
    path('help/', views.help_page, name='help'),
    path('verify/', report_views.verify_receipt, name='verify_receipt'),
    path('private/<str:kind>/<int:pk>/', views.private_file, name='private_file'),
    path('admin/', admin.site.urls),
    path('login/', account_views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('password-reset/', account_views.PasswordResetView.as_view(), name='password_reset'),
    path('password-reset/phone/', account_views.password_reset_phone, name='password_reset_phone'),
    path('password-reset/sent/', auth_views.PasswordResetDoneView.as_view(), name='password_reset_done'),
    path('password-reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(),
         name='password_reset_confirm'),
    path('password-reset/done/', auth_views.PasswordResetCompleteView.as_view(), name='password_reset_complete'),
    path('', include('listings.urls')),
    path('', include(staff_patterns)),
    path('', include('accounts.urls')),
    path('payments/', include('payments.urls')),
    path('maintenance/', include('maintenance.urls')),
    path('messages/', include('notifications.urls')),
    path('reports/', include('reports.urls')),
    path('activity/', include('audit.urls')),
    path('announcements/', include('announcements.urls')),
    path('support/impersonate/', impersonation.choose, name='impersonate'),
    path('support/impersonate/<int:pk>/start/', impersonation.start, name='impersonate_start'),
    path('support/impersonate/stop/', impersonation.stop, name='impersonate_stop'),
    path('', include('support.urls')),
    path('', include('billing.urls')),
    path('settings/business/', business_details, name='business_details'),
    path('', include('rentals.urls')),
]

if settings.DEBUG:  # on the server, Caddy serves uploaded files
    from django.conf.urls.static import static

    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
