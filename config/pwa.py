"""Installable app (PWA): manifests, the service worker and the offline page."""

import hashlib
import json

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.cache import cache_control

from rentals.models import Tenant

THEME = '#0e7a4b'
BACKGROUND = '#f6f7f9'


def _icons():
    return [
        {'src': static('img/icons/icon-192.png'), 'sizes': '192x192', 'type': 'image/png'},
        {'src': static('img/icons/icon-512.png'), 'sizes': '512x512', 'type': 'image/png'},
        {'src': static('img/icons/icon-maskable-512.png'), 'sizes': '512x512', 'type': 'image/png',
         'purpose': 'maskable'},
    ]


def _manifest_response(data):
    return HttpResponse(json.dumps(data, indent=2), content_type='application/manifest+json')


@cache_control(max_age=3600)
def manifest(request):
    """The landlord app: opens on the dashboard."""
    return _manifest_response({
        'id': '/?app=landlord',
        'name': settings.APP_NAME,
        'short_name': settings.APP_NAME,
        'description': 'Collect rent on M-Pesa, track arrears and manage your rentals.',
        'start_url': reverse('rentals:dashboard') + '?source=app',
        'scope': '/',
        'display': 'standalone',
        'background_color': BACKGROUND,
        'theme_color': THEME,
        'icons': _icons(),
        'shortcuts': [
            {'name': 'Payments', 'url': reverse('payments:payment_list'), 'icons': _icons()[:1]},
            {'name': 'Leases', 'url': reverse('rentals:lease_list'), 'icons': _icons()[:1]},
            {'name': 'New announcement', 'url': reverse('announcements:create'), 'icons': _icons()[:1]},
        ],
    })


@cache_control(max_age=3600)
def tenant_manifest(request, token):
    """A tenant's own app: opens straight on their private rent page, never the landlord login."""
    tenant = get_object_or_404(Tenant, portal_token=token)
    home = reverse('rentals:tenant_portal', args=[token])
    return _manifest_response({
        'id': home,
        'name': f'My rent · {settings.APP_NAME}',
        'short_name': 'My rent',
        'description': f'Rent, payments and repairs for {tenant.full_name.split()[0]}.',
        'start_url': home,
        'scope': home,
        'display': 'standalone',
        'background_color': BACKGROUND,
        'theme_color': THEME,
        'icons': _icons(),
    })


@cache_control(no_cache=True)
def service_worker(request):
    """Served from the site root so it can work for every page. Caches only static files."""
    precache = [
        static('css/app.css'), static('css/features.css'), static('js/app.js'), static('js/theme.js'),
        static('js/live.js'), static('img/logo-transparent.png'), static('img/logo-on-dark.png'),
        static('img/icons/icon-192.png'),
    ]
    # Static file names carry a content hash in production, so this changes whenever they do.
    version = hashlib.sha256('|'.join(precache).encode()).hexdigest()[:12]
    response = render(request, 'pwa/sw.js', {'precache': precache, 'version': version},
                      content_type='application/javascript')
    response['Service-Worker-Allowed'] = '/'
    return response


def offline(request):
    return render(request, 'pwa/offline.html')
