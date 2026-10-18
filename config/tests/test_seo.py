from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from ops.models import SiteSettings
from rentals.tests import make_lease


@override_settings(SITE_URL='https://rent.example.com')
class SitemapTests(TestCase):
    def test_lists_public_pages_with_the_real_domain(self):
        resp = self.client.get('/sitemap.xml', HTTP_HOST='localhost')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/xml')
        body = resp.content.decode()
        for path in ['/', '/signup/', '/login/', '/contact/']:
            self.assertIn(f'<loc>https://rent.example.com{path}</loc>', body)
        self.assertNotIn('localhost', body)  # never the host a request came in on

    def test_never_lists_private_pages(self):
        owner = get_user_model().objects.create_user('landlord')
        token = make_lease(owner).tenant.portal_token
        body = self.client.get('/sitemap.xml').content.decode()
        for private in ['/t/', token, '/dashboard/', '/admin/', '/leases/', '/payments/']:
            self.assertNotIn(private, body)


@override_settings(SITE_URL='https://rent.example.com')
class RobotsTests(TestCase):
    def test_robots_txt(self):
        resp = self.client.get('/robots.txt')
        self.assertEqual(resp['Content-Type'], 'text/plain; charset=utf-8')
        body = resp.content.decode()
        self.assertIn('Sitemap: https://rent.example.com/sitemap.xml', body)
        for private in ['/admin/', '/t/', '/dashboard/', '/payments/', '/reports/']:
            self.assertIn(f'Disallow: {private}', body)
        self.assertNotIn('Disallow: /\n', body)  # the public site stays crawlable


@override_settings(SITE_URL='https://rent.example.com')
class RobotsMetaTests(TestCase):
    def test_public_pages_are_indexable_with_link_previews(self):
        for url in [reverse('rentals:home'), reverse('accounts:signup'), reverse('login'), reverse('support:contact')]:
            resp = self.client.get(url)
            self.assertContains(resp, '<meta name="robots" content="index, follow">', msg_prefix=url)
            self.assertNotContains(resp, 'noindex', msg_prefix=url)
            self.assertContains(resp, f'<link rel="canonical" href="https://rent.example.com{url}">', msg_prefix=url)
        home = self.client.get(reverse('rentals:home'))
        self.assertContains(home, '<meta property="og:title" content="Collect rent on M-Pesa · Rentipay">')
        self.assertContains(home, 'content="https://rent.example.com/static/img/logo.jpeg"')

    def test_private_pages_are_noindex(self):
        owner = get_user_model().objects.create_user('landlord')
        lease = make_lease(owner)
        self.assertContains(self.client.get(reverse('rentals:tenant_portal', args=[lease.tenant.portal_token])),
                            '<meta name="robots" content="noindex, nofollow">')
        self.client.force_login(owner)
        dash = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(dash, '<meta name="robots" content="noindex, nofollow">')
        self.assertNotContains(dash, 'og:title')


class SocialLinksTests(TestCase):
    """Set by an administrator in Settings → Business details."""

    def tearDown(self):
        cache.delete(SiteSettings.CACHE_KEY)

    def test_footer_shows_only_the_links_that_are_set(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={'facebook': 'https://facebook.com/rentipay',
                                                              'whatsapp': '0712 345 678'})
        page = self.client.get('/')
        self.assertContains(page, 'href="https://facebook.com/rentipay"')
        self.assertContains(page, 'href="https://wa.me/254712345678"')
        self.assertContains(page, 'on WhatsApp')
        self.assertNotContains(page, 'on Instagram')

    def test_no_links_no_icons(self):
        cache.delete(SiteSettings.CACHE_KEY)
        self.assertNotContains(self.client.get('/'), 'social-links')


class DescriptionsAndStructuredDataTests(TestCase):
    """Each public page has its own description (search results and link previews), plus structured data."""

    def ld(self, html):
        import json
        import re

        return [json.loads(b) for b in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)]

    def test_each_public_page_describes_itself(self):
        from rentals.utils import SEO_DESCRIPTIONS

        seen = set()
        for name, text in SEO_DESCRIPTIONS.items():
            self.assertLessEqual(len(text), 160, name)
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn(f'<meta name="description" content="{escape(text)}">', html, name)
            self.assertIn(f'<meta property="og:description" content="{escape(text)}">', html, name)
            seen.add(text)
        self.assertEqual(len(seen), len(SEO_DESCRIPTIONS))  # no two pages share one

    def test_home_describes_the_app(self):
        data = self.ld(self.client.get(reverse('rentals:home')).content.decode())[0]
        types = [g['@type'] for g in data['@graph']]
        self.assertEqual(types, ['Organization', 'WebSite', 'SoftwareApplication'])
        app = data['@graph'][2]
        self.assertEqual((app['offers']['priceCurrency'], app['areaServed']['name']), ('KES', 'Kenya'))

    def test_help_questions_become_an_faq(self):
        html = self.client.get(reverse('help')).content.decode()
        [faq] = self.ld(html)
        self.assertEqual(faq['@type'], 'FAQPage')
        self.assertGreater(len(faq['mainEntity']), 20)
        first = faq['mainEntity'][0]
        self.assertTrue(first['name'].endswith('?'))
        self.assertNotIn('<', first['acceptedAnswer']['text'])  # plain text, no markup

    def test_no_google_fonts(self):
        html = self.client.get(reverse('rentals:home')).content.decode()
        self.assertNotIn('fonts.googleapis.com', html)
        self.assertIn('inter-latin', html)
