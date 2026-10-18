"""Business details set in the app, the help page, billing in the terms, and accepting updated terms."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.models import Profile, StaffMember
from billing.models import BillingSettings
from ops.models import SiteSettings


class CacheCleanup(TestCase):
    def tearDown(self):
        # The database is rolled back after each test; cached settings must go too.
        cache.delete(SiteSettings.CACHE_KEY)
        cache.delete(BillingSettings.CACHE_KEY)


class BusinessDetailsTests(CacheCleanup):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('admin', 'admin@example.com', 'pw')
        self.url = reverse('business_details')

    def test_administrator_sets_details_shown_on_the_site(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('accounts:settings')), 'Business details')
        resp = self.client.post(self.url, {'legal_name': 'Rentipay Ltd', 'address': 'P.O. Box 1, Nairobi',
                                           'privacy_email': 'privacy@rentipay.example', 'support_phone': '0712 345 678',
                                           'whatsapp': '0712345678', 'instagram': 'https://instagram.com/rentipay'})
        self.assertRedirects(resp, self.url)
        terms = self.client.get(reverse('terms'))
        for text in ('Rentipay Ltd', 'P.O. Box 1, Nairobi', 'privacy@rentipay.example'):
            self.assertContains(terms, text)
        home = self.client.get('/')
        self.assertContains(home, 'href="https://instagram.com/rentipay"')
        self.assertContains(home, 'href="https://wa.me/254712345678"')
        help_page = self.client.get(reverse('help'))
        self.assertContains(help_page, 'WhatsApp 0712345678')
        self.assertContains(help_page, 'Call 0712 345 678')

    def test_bad_whatsapp_number(self):
        self.client.force_login(self.admin)
        resp = self.client.post(self.url, {'whatsapp': '123'})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(SiteSettings.objects.filter(whatsapp='123').exists())

    def test_landlords_cannot_change_it(self):
        self.client.force_login(get_user_model().objects.create_user('landlord'))
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, {'legal_name': 'Hacked'}).status_code, 404)


class HelpPageTests(CacheCleanup):
    def test_public_and_linked(self):
        page = self.client.get(reverse('help'))
        self.assertContains(page, 'Upload M-Pesa statement')
        self.assertContains(page, 'Send us a message')
        self.assertNotContains(page, 'How much does')  # billing answers only once billing is on
        self.assertContains(self.client.get('/'), f'href="{reverse("help")}"')
        self.assertIn('/help/', self.client.get('/sitemap.xml').content.decode())

    def test_billing_answers_use_the_live_prices(self):
        BillingSettings.objects.update_or_create(pk=1, defaults={'enabled': True, 'price_per_unit': 90, 'free_units': 4})
        page = self.client.get(reverse('help'))
        self.assertContains(page, 'Your first 4 occupied units are free; each further occupied unit is KES 90 a month')


class TermsTests(CacheCleanup):
    def test_terms_explain_billing(self):
        terms = self.client.get(reverse('terms'))
        for text in ('Fees and billing', 'currently free to use', 'never delete your data because a bill is late',
                     'at least 30 days before a price increase'):
            self.assertContains(terms, text)
        BillingSettings.objects.update_or_create(pk=1, defaults={'enabled': True, 'price_per_unit': 75, 'free_units': 5})
        self.assertContains(self.client.get(reverse('terms')),
                            'Your first 5 occupied units are free, and each\n    further occupied unit costs KES 75 a month.')
        privacy = self.client.get(reverse('privacy'))
        self.assertContains(privacy, 'Ireland')
        self.assertContains(privacy, '90 days in separate off-site')


class AcceptUpdatedTermsTests(CacheCleanup):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        Profile.objects.create(user=self.owner, terms_version='2026-10-14')  # accepted the previous terms
        self.client.force_login(self.owner)

    def test_asked_once_then_carries_on(self):
        dashboard = reverse('rentals:dashboard')
        resp = self.client.get(dashboard)
        self.assertRedirects(resp, f"{reverse('accounts:accept_terms')}?next={dashboard}")
        page = self.client.get(resp.url)
        self.assertContains(page, "We've updated our terms")
        self.assertEqual(self.client.get(reverse('terms')).status_code, 200)  # can read them first

        self.assertContains(self.client.post(reverse('accounts:accept_terms'), {'next': dashboard}), 'Tick the box')
        resp = self.client.post(reverse('accounts:accept_terms'), {'next': dashboard, 'accept': '1'})
        self.assertRedirects(resp, dashboard)
        profile = Profile.objects.get(user=self.owner)
        self.assertEqual(profile.terms_version, settings.LEGAL['TERMS_VERSION'])
        self.assertIsNotNone(profile.terms_accepted_at)
        self.assertEqual(self.client.get(dashboard).status_code, 200)

    def test_changes_wait_until_accepted(self):
        resp = self.client.post(reverse('rentals:property_create'), {'name': 'New'})
        self.assertRedirects(resp, reverse('accounts:accept_terms'))

    def test_next_must_stay_on_this_site(self):
        resp = self.client.post(reverse('accounts:accept_terms'), {'next': 'https://evil.example', 'accept': '1'})
        self.assertRedirects(resp, reverse('rentals:dashboard'), fetch_redirect_response=False)

    def test_not_asked(self):
        """New sign-ups, administrator-made accounts, staff and administrators aren't interrupted."""
        Profile.objects.filter(user=self.owner).update(terms_version=settings.LEGAL['TERMS_VERSION'])
        self.assertEqual(self.client.get(reverse('rentals:dashboard')).status_code, 200)
        made_by_admin = get_user_model().objects.create_user('made')
        self.client.force_login(made_by_admin)
        self.assertEqual(self.client.get(reverse('rentals:dashboard')).status_code, 200)
        member = StaffMember.objects.create(owner=self.owner, user=get_user_model().objects.create_user('caretaker'))
        Profile.objects.create(user=member.user, terms_version='2026-10-14')
        self.client.force_login(member.user)
        self.assertEqual(self.client.get(reverse('staff:home')).status_code, 200)


class HomeHeaderTests(TestCase):
    def test_logged_in_visitors_can_log_out_from_the_home_page(self):
        user = get_user_model().objects.create_user('landlord')
        self.client.force_login(user)
        page = self.client.get(reverse('rentals:home'))
        self.assertContains(page, 'Open dashboard')
        self.assertContains(page, f'action="{reverse("logout")}"')
        self.assertRedirects(self.client.post(reverse('logout')), reverse('rentals:home'))
        self.assertContains(self.client.get(reverse('rentals:home')), 'Get started')
