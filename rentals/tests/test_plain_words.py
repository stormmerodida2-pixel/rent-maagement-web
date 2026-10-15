"""One Tenants menu item with plain words, confirmation pop-ups, and setting each unit's rent."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from ..models import Property, Unit
from . import make_lease


class PlainWordsTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner, label='A1')
        self.client.force_login(self.owner)

    def test_one_tenants_menu_item(self):
        page = self.client.get(reverse('rentals:dashboard')).content.decode()
        nav = page.split('aria-label="Main"')[1].split('</nav>')[0]
        self.assertIn('>Tenants</a>', nav.replace('\n', ''))
        self.assertNotIn('Leases', nav)
        self.assertIn('Move a tenant in', page)
        tenants = self.client.get(reverse('rentals:lease_list'))
        self.assertContains(tenants, 'Living here')
        self.assertContains(tenants, 'Moved out')

    def test_tenancy_page_says_move_out(self):
        page = self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk]))
        self.assertContains(page, 'Living here')
        self.assertContains(page, '>Move out</a>')
        self.assertContains(page, 'Edit rent &amp; details')
        self.assertNotContains(page, 'End lease')


class PopupTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner, label='A1')
        self.client.force_login(self.owner)

    def test_risky_actions_ask_first(self):
        page = self.client.get(reverse('rentals:lease_end', args=[self.lease.pk]))
        self.assertContains(page, 'data-confirm="')
        self.assertContains(page, "can&#x27;t be undone")
        self.assertContains(page, 'vendor/sweetalert2.all.min.js')
        self.assertTrue(finders.find('vendor/sweetalert2.all.min.js'))

    def test_success_messages_become_toasts_but_errors_stay(self):
        resp = self.client.post(reverse('rentals:property_rents', args=[self.lease.unit.property_id]),
                                {f'rent-{self.lease.unit_id}': '16000', f'bedrooms-{self.lease.unit_id}': '1'}, follow=True)
        self.assertContains(resp, 'data-toast="success"')


class SetRentsTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.prop = Property.objects.create(owner=self.owner, name='Riverside')
        self.b1 = Unit.objects.create(property=self.prop, label='B1', default_rent=18000)
        self.b2 = Unit.objects.create(property=self.prop, label='B2', default_rent=18000)
        self.client.force_login(self.owner)
        self.url = reverse('rentals:property_rents', args=[self.prop.pk])

    def test_units_can_have_different_rents_and_types(self):
        lease = make_lease(self.owner, label='X')  # someone elsewhere: unaffected
        resp = self.client.post(self.url, {
            f'rent-{self.b1.pk}': '15000', f'bedrooms-{self.b1.pk}': '0', f'type-{self.b1.pk}': 'Bedsitter',
            f'rent-{self.b2.pk}': '25,000', f'bedrooms-{self.b2.pk}': '2', f'type-{self.b2.pk}': '2-bedroom',
        })
        self.assertRedirects(resp, reverse('rentals:property_detail', args=[self.prop.pk]))
        self.b1.refresh_from_db()
        self.b2.refresh_from_db()
        self.assertEqual((self.b1.default_rent, self.b1.unit_type, self.b1.bedrooms), (Decimal('15000'), 'Bedsitter', 0))
        self.assertEqual((self.b2.default_rent, self.b2.unit_type), (Decimal('25000'), '2-bedroom'))
        lease.refresh_from_db()
        self.assertEqual(lease.monthly_rent, Decimal('15000'))  # make_lease default, untouched
        detail = self.client.get(reverse('rentals:property_detail', args=[self.prop.pk]))
        self.assertContains(detail, 'Bedsitter')

    def test_a_bad_row_saves_nothing(self):
        resp = self.client.post(self.url, {
            f'rent-{self.b1.pk}': '15000', f'bedrooms-{self.b1.pk}': '1',
            f'rent-{self.b2.pk}': 'abc', f'bedrooms-{self.b2.pk}': '1',
        })
        self.assertContains(resp, 'Check this row')
        self.b1.refresh_from_db()
        self.assertEqual(self.b1.default_rent, Decimal('18000'))

    def test_current_tenants_keep_their_rent(self):
        lease = make_lease(self.owner, label='B3', rent='18000')
        Unit.objects.filter(pk=lease.unit_id).update(property=self.prop)
        data = {}
        for u in self.prop.units.all():
            data.update({f'rent-{u.pk}': '30000', f'bedrooms-{u.pk}': '1'})
        self.client.post(self.url, data)
        lease.refresh_from_db()
        self.assertEqual(lease.monthly_rent, Decimal('18000'))

    def test_private(self):
        self.client.force_login(get_user_model().objects.create_user('other'))
        self.assertEqual(self.client.get(self.url).status_code, 404)


class GettingStartedTests(TestCase):
    def test_setup_lives_on_the_dashboard_not_the_menu(self):
        owner = get_user_model().objects.create_user('newlandlord', password='pw')
        self.client.force_login(owner)
        page = self.client.get(reverse('rentals:dashboard')).content.decode()
        nav = page.split('aria-label="Main"')[1].split('</nav>')[0]
        self.assertNotIn('Getting started', nav)
        self.assertIn('Continue setup', page)
