from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path

from accounts import views as account_views
from ops.views import healthz

from . import pwa
from .seo import SITEMAPS, robots_txt

admin.site.site_header = settings.APP_NAME
admin.site.site_title = settings.APP_NAME
admin.site.index_title = 'Manage your rentals'
# The admin's own login page gets the same password-guessing limits as the app's.
admin.site.login = account_views.throttle_login(admin.site.login)

urlpatterns = [
    path('healthz/', healthz, name='healthz'),
    path('robots.txt', robots_txt, name='robots_txt'),
    path('manifest.webmanifest', pwa.manifest, name='manifest'),
    path('t/<str:token>/manifest.webmanifest', pwa.tenant_manifest, name='tenant_manifest'),
    path('sw.js', pwa.service_worker, name='service_worker'),
    path('offline/', pwa.offline, name='offline'),
    path('sitemap.xml', sitemap, {'sitemaps': SITEMAPS}, name='sitemap'),
    path('admin/', admin.site.urls),
    path('login/', account_views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('password-reset/', account_views.PasswordResetView.as_view(), name='password_reset'),
    path('password-reset/sent/', auth_views.PasswordResetDoneView.as_view(), name='password_reset_done'),
    path('password-reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(),
         name='password_reset_confirm'),
    path('password-reset/done/', auth_views.PasswordResetCompleteView.as_view(), name='password_reset_complete'),
    path('', include('accounts.urls')),
    path('payments/', include('payments.urls')),
    path('maintenance/', include('maintenance.urls')),
    path('messages/', include('notifications.urls')),
    path('reports/', include('reports.urls')),
    path('activity/', include('audit.urls')),
    path('announcements/', include('announcements.urls')),
    path('', include('support.urls')),
    path('', include('rentals.urls')),
]
