"""Search engines: a sitemap of the public pages, and robots.txt keeping crawlers out of private areas."""

from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.sitemaps import Sitemap
from django.http import HttpResponse
from django.urls import reverse
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_GET


class PublicPagesSitemap(Sitemap):
    """Only pages anyone may see. Dashboards, the admin and tenants' private links are never listed."""

    PAGES = {
        # url name: (priority, how often it changes)
        'rentals:home': (1.0, 'weekly'),
        'accounts:signup': (0.8, 'monthly'),
        'login': (0.5, 'yearly'),
        'support:contact': (0.4, 'yearly'),
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


SITEMAPS = {'pages': PublicPagesSitemap}

# Private areas. They need a login (or a secret tenant link) anyway; this keeps crawlers from trying.
PRIVATE_PATHS = [
    '/admin/', '/t/', '/dashboard/', '/welcome/', '/properties/', '/units/', '/tenants/', '/leases/',
    '/charges/', '/readings/', '/payments/', '/maintenance/', '/complaints/', '/messages/', '/reports/',
    '/activity/', '/announcements/', '/verify-email/', '/password-reset/', '/logout/', '/healthz/',
]


@require_GET
@cache_page(60 * 60)
def robots_txt(request):
    lines = ['User-agent: *', 'Allow: /']
    lines += [f'Disallow: {path}' for path in PRIVATE_PATHS]
    lines += ['', f"Sitemap: {settings.SITE_URL}{reverse('sitemap')}"]
    return HttpResponse('\n'.join(lines) + '\n', content_type='text/plain; charset=utf-8')
