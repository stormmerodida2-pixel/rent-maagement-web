from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(SITE_URL='https://rent.example.com',
                   LEGAL={'ENTITY': 'Liskodi Ltd', 'ADDRESS': 'P.O. Box 1, Nairobi', 'EMAIL': 'privacy@rent.example.com',
                          'UPDATED': 'October 14, 2026', 'TERMS_VERSION': '2026-10-14'})
class LegalPageTests(TestCase):
    def test_privacy_policy(self):
        resp = self.client.get(reverse('privacy'))
        self.assertContains(resp, '<h1>Privacy policy</h1>', html=True)
        for text in ['Data Protection Act, 2019', 'data controller', 'data processor', 'Safaricom',
                     "Africa's Talking", 'www.odpc.go.ke', '72 hours', 'Liskodi Ltd', 'P.O. Box 1, Nairobi',
                     'privacy@rent.example.com']:
            self.assertContains(resp, text)

    def test_terms(self):
        resp = self.client.get(reverse('terms'))
        self.assertContains(resp, '<h1>Terms of service</h1>', html=True)
        for text in ['directly into your own Paybill or Till', 'governed by the laws of Kenya', 'Liskodi Ltd']:
            self.assertContains(resp, text)

    def test_public_indexable_and_in_sitemap(self):
        for name in ['privacy', 'terms']:
            resp = self.client.get(reverse(name))
            self.assertContains(resp, '<meta name="robots" content="index, follow">')
            self.assertContains(resp, f'https://rent.example.com{reverse(name)}')
        sitemap = self.client.get('/sitemap.xml').content.decode()
        self.assertIn('https://rent.example.com/privacy/', sitemap)
        self.assertIn('https://rent.example.com/terms/', sitemap)

    def test_linked_from_public_pages_and_signup(self):
        for url in [reverse('rentals:home'), reverse('login'), reverse('accounts:signup')]:
            resp = self.client.get(url)
            self.assertContains(resp, f'href="{reverse("privacy")}"', msg_prefix=url)
            self.assertContains(resp, f'href="{reverse("terms")}"', msg_prefix=url)
