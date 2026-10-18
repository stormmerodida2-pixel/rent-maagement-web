"""Site-wide pages that belong to no app: search-engine files, legal pages and the installable-app files."""

import hashlib
import json
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.sitemaps import Sitemap
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import ManifestFilesMixin, staticfiles_storage
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils.translation import gettext
from django.views.decorators.cache import cache_control, cache_page
from django.views.decorators.http import require_GET
from django.views.generic import TemplateView

from rentals.models import Tenant
from rentals.utils import tenant_language


# --- Search engines: sitemap and robots.txt ----------------------------------------------------------

# Search engines: a sitemap of the public pages, and robots.txt keeping crawlers out of private areas.

class PublicPagesSitemap(Sitemap):
    """Only pages anyone may see. Dashboards, the admin and tenants' private links are never listed."""

    PAGES = {
        # url name: (priority, how often it changes)
        'rentals:home': (1.0, 'weekly'),
        'accounts:signup': (0.8, 'monthly'),
        'login': (0.5, 'yearly'),
        'support:contact': (0.4, 'yearly'),
        'help': (0.5, 'monthly'),
        'privacy': (0.3, 'yearly'),
        'terms': (0.3, 'yearly'),
    }

    def items(self):
        return list(self.PAGES)

    def location(self, name):
        return reverse(name)

    def priority(self, name):
        return self.PAGES[name][0]

    def changefreq(self, name):
        return self.PAGES[name][1]

    # Always the real public address from SITE_URL, never whatever host a request came in on.
    def get_domain(self, site=None):
        return urlsplit(settings.SITE_URL).netloc

    def get_protocol(self, protocol=None):
        return urlsplit(settings.SITE_URL).scheme or 'https'


class ListingsSitemap(PublicPagesSitemap):
    """Open vacancy listings, so people searching for a house can find them."""

    def items(self):
        from listings.models import Listing

        return Listing.objects.filter(is_open=True).order_by('-created_at')

    def location(self, listing):
        return listing.get_absolute_url()

    def priority(self, listing):
        return 0.6

    def changefreq(self, listing):
        return 'daily'

    def lastmod(self, listing):
        return listing.created_at


SITEMAPS = {'pages': PublicPagesSitemap, 'listings': ListingsSitemap}

# Private areas. They need a login (or a secret tenant link) anyway; this keeps crawlers from trying.
PRIVATE_PATHS = [
    '/admin/', '/t/', '/dashboard/', '/welcome/', '/properties/', '/units/', '/tenants/', '/leases/',
    '/charges/', '/readings/', '/payments/', '/maintenance/', '/complaints/', '/messages/', '/reports/',
    '/activity/', '/announcements/', '/verify-email/', '/password-reset/', '/logout/', '/healthz/',
    '/vacancies/', '/staff/', '/settings/', '/private/', '/billing/', '/support/',
]


@require_GET
@cache_page(60 * 60)
def robots_txt(request):
    lines = ['User-agent: *', 'Allow: /']
    lines += [f'Disallow: {path}' for path in PRIVATE_PATHS]
    lines += ['', f"Sitemap: {settings.SITE_URL}{reverse('sitemap')}"]
    return HttpResponse('\n'.join(lines) + '\n', content_type='text/plain; charset=utf-8')


# --- Privacy policy and terms ------------------------------------------------------------------------

class LegalPage(TemplateView):
    page_title = ''

    def get_context_data(self, **kwargs):
        from billing.models import BillingSettings
        from ops.models import SiteSettings

        site = SiteSettings.load()
        return super().get_context_data(legal=site.legal(), site=site, billing=BillingSettings.public(),
                                        page_title=self.page_title, **kwargs)


privacy = LegalPage.as_view(template_name='legal/privacy.html', page_title='Privacy policy')
class HelpPage(LegalPage):
    """The help page, with its questions and answers also given to search engines as an FAQ (they can show them
    right in the search results)."""

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['faq_data'] = None  # filled in from the rendered questions in render_to_response
        return ctx

    def render_to_response(self, context, **kwargs):
        import html
        import re

        from django.template.loader import render_to_string
        from django.utils.html import strip_tags

        body = render_to_string(self.template_name, context, request=self.request)
        pairs = re.findall(r'<details><summary>(.*?)</summary>\s*<p>(.*?)</p></details>', body, flags=re.S)
        context['faq_data'] = {'@context': 'https://schema.org', '@type': 'FAQPage', 'mainEntity': [
            {'@type': 'Question', 'name': html.unescape(strip_tags(q)).strip(),
             'acceptedAnswer': {'@type': 'Answer', 'text': ' '.join(html.unescape(strip_tags(a)).split())}}
            for q, a in pairs]} if pairs else None
        return super().render_to_response(context, **kwargs)


help_page = HelpPage.as_view(template_name='help.html', page_title='Help')
terms = LegalPage.as_view(template_name='legal/terms.html', page_title='Terms of service')


# --- Installable app: manifests, service worker, offline page ----------------------------------------

# Installable app (PWA): manifests, the service worker and the offline page.

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
    with tenant_language(tenant):
        short_name = gettext('My rent')
        description = gettext('Rent, payments and repairs for %(name)s.') % {'name': tenant.full_name.split()[0]}
    return _manifest_response({
        'id': home,
        'lang': tenant.language,
        'name': f'{short_name} · {settings.APP_NAME}',
        'short_name': short_name,
        'description': description,
        'start_url': home,
        'scope': home,
        'display': 'standalone',
        'background_color': BACKGROUND,
        'theme_color': THEME,
        'icons': _icons(),
    })


def _contents_fingerprint(urls):
    parts = []
    for url in urls:
        path = finders.find(url.removeprefix(settings.STATIC_URL).removeprefix('/' + settings.STATIC_URL))
        parts.append(hashlib.sha256(open(path, 'rb').read()).hexdigest() if path else url)
    return '|'.join(parts)


@cache_control(no_cache=True)
def service_worker(request):
    """Served from the site root so it can work for every page. Caches only static files."""
    precache = [
        static('css/app.css'), static('css/features.css'), static('js/app.js'), static('js/theme.js'),
        static('js/live.js'), static('img/logo-transparent.png'), static('img/logo-on-dark.png'),
        static('img/icons/icon-192.png'),
    ]
    # In production static file names carry a content hash, so a cached copy is always right and the
    # version changes whenever a file does. Without hashing (local development) a file keeps its name
    # when it changes, so the worker must check the server first and the version follows the contents.
    hashed = isinstance(staticfiles_storage, ManifestFilesMixin)
    fingerprint = '|'.join(precache) if hashed else _contents_fingerprint(precache)
    version = hashlib.sha256(fingerprint.encode()).hexdigest()[:12]
    response = render(request, 'pwa/sw.js', {'precache': precache, 'version': version, 'cache_first': hashed},
                      content_type='application/javascript')
    response['Service-Worker-Allowed'] = '/'
    return response


def offline(request):
    return render(request, 'pwa/offline.html')


# --- Private files (receipts, meter photos) --------------------------------------------------------------

@login_required
def private_file(request, kind, pk):
    """A receipt or meter photo, for the landlord it belongs to or the caretaker who took it. Never cached publicly."""
    from django.http import FileResponse, Http404

    from accounts.models import staff_for
    from payments.models import StaffExpense
    from rentals.models import MeterReading

    staff = staff_for(request.user)
    if kind == 'receipt':
        item = StaffExpense.objects.filter(pk=pk).first()
        field = item.receipt if item else None
        allowed = item and (item.owner_id == request.user.pk or (staff and item.staff_id == staff.pk))
    elif kind == 'meter':
        item = MeterReading.all_objects.select_related('unit__property').filter(pk=pk).first()
        field = item.photo if item else None
        allowed = item and (item.unit.property.owner_id == request.user.pk or
                            (staff and item.recorded_by_id == request.user.pk))
    elif kind == 'inspection':
        from rentals.models import InspectionPhoto

        item = InspectionPhoto.objects.filter(pk=pk, inspection__lease__unit__property__owner=request.user).first()
        field = item.photo if item else None
        allowed = item is not None
    elif kind == 'expense':
        from payments.models import Expense

        item = Expense.all_objects.filter(pk=pk, owner=request.user).first()
        field = item.receipt if item else None
        allowed = item is not None
    else:
        raise Http404
    if not allowed or not field:
        raise Http404
    response = FileResponse(field.open('rb'), content_type='image/jpeg')
    response['Cache-Control'] = 'private, max-age=3600'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
