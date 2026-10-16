"""Landlord-facing pages: properties, units, tenants and leases."""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from payments.models import Payment

from ..models import Lease, Property, RentCharge, Tenant, Unit
from . import make_lease


class PageTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user('alice', password='pw', first_name='Alice')
        self.bob = User.objects.create_user('bob', password='pw')
        self.client.force_login(self.alice)


class PropertyPagesTests(PageTestCase):
    def test_create_property_with_quick_units(self):
        resp = self.client.post(reverse('rentals:property_create'), {
            'name': 'Sunrise', 'address': 'Kilimani', 'unit_prefix': 'A', 'unit_count': '3', 'unit_rent': '15000',
        })
        prop = Property.objects.get()
        self.assertRedirects(resp, reverse('rentals:property_detail', args=[prop.pk]))
        self.assertEqual(prop.owner, self.alice)
        self.assertEqual(sorted(prop.units.values_list('label', flat=True)), ['A1', 'A2', 'A3'])
        self.assertEqual(set(prop.units.values_list('default_rent', flat=True)), {Decimal('15000')})

    def test_unit_count_requires_rent(self):
        resp = self.client.post(reverse('rentals:property_create'), {'name': 'X', 'unit_count': '2'})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Property.objects.exists())

    def test_list_shows_occupancy_and_only_own_properties(self):
        make_lease(self.alice)
        make_lease(self.bob, label='B1')
        resp = self.client.get(reverse('rentals:property_list'))
        self.assertEqual([p.owner for p in resp.context['object_list']], [self.alice])
        self.assertEqual((resp.context['object_list'][0].occupied, resp.context['object_list'][0].vacant), (1, 0))

    def test_cannot_view_edit_or_delete_another_landlords_property(self):
        bob_prop = make_lease(self.bob).unit.property
        for name in ['property_detail', 'property_update', 'property_delete']:
            self.assertEqual(self.client.get(reverse(f'rentals:{name}', args=[bob_prop.pk])).status_code, 404, name)
        self.assertEqual(self.client.post(reverse('rentals:unit_create', args=[bob_prop.pk]),
                                          {'label': 'X', 'bedrooms': 1, 'default_rent': 1}).status_code, 404)

    def test_add_unit_rejects_duplicate_label(self):
        prop = Property.objects.create(owner=self.alice, name='P')
        url = reverse('rentals:unit_create', args=[prop.pk])
        self.client.post(url, {'label': 'A1', 'unit_type': 'single', 'default_rent': '9000'})
        resp = self.client.post(url, {'label': 'a1', 'unit_type': 'single', 'default_rent': '9000'})
        self.assertContains(resp, 'already has a unit called a1')
        self.assertEqual(prop.units.count(), 1)

    def test_deleting_property_with_leases_is_refused(self):
        prop = make_lease(self.alice).unit.property
        resp = self.client.post(reverse('rentals:property_delete', args=[prop.pk]), follow=True)
        self.assertContains(resp, 'has units with leases')
        self.assertTrue(Property.objects.filter(pk=prop.pk).exists())

    def test_delete_empty_unit(self):
        prop = Property.objects.create(owner=self.alice, name='P')
        unit = Unit.objects.create(property=prop, label='A1', default_rent=1)
        self.client.post(reverse('rentals:unit_delete', args=[unit.pk]))
        self.assertFalse(Unit.objects.exists())


class TenantPagesTests(PageTestCase):
    def test_create_tenant_normalizes_phone_and_goes_to_lease(self):
        resp = self.client.post(reverse('rentals:tenant_create'), {'full_name': 'Wanjiku K', 'phone': '0712 345 678'})
        tenant = Tenant.objects.get()
        self.assertEqual((tenant.owner, tenant.phone), (self.alice, '254712345678'))
        self.assertRedirects(resp, reverse('rentals:lease_create') + f'?tenant={tenant.pk}')

    def test_invalid_phone(self):
        resp = self.client.post(reverse('rentals:tenant_create'), {'full_name': 'X', 'phone': '123'})
        self.assertContains(resp, 'not a valid Kenyan mobile number')

    def test_search_and_scoping(self):
        make_lease(self.alice, label='A1')
        make_lease(self.bob, label='B1')
        resp = self.client.get(reverse('rentals:tenant_list'))
        self.assertEqual([t.owner for t in resp.context['object_list']], [self.alice])
        self.assertEqual(len(self.client.get(reverse('rentals:tenant_list') + '?q=nobody').context['rows']), 0)

    def test_next_url_is_honoured_but_not_offsite(self):
        tenant = make_lease(self.alice).tenant
        url = reverse('rentals:tenant_update', args=[tenant.pk])
        data = {'full_name': 'New Name', 'phone': tenant.phone}
        self.assertRedirects(self.client.post(url + '?next=/dashboard/', data), '/dashboard/')
        resp = self.client.post(url + '?next=https://evil.example/', data)
        self.assertRedirects(resp, reverse('rentals:tenant_list'))


class LeasePagesTests(PageTestCase):
    def setUp(self):
        super().setUp()
        self.prop = Property.objects.create(owner=self.alice, name='P')
        self.unit = Unit.objects.create(property=self.prop, label='A1', default_rent=12000)
        self.tenant = Tenant.objects.create(owner=self.alice, full_name='T', phone='0712345678')

    def post_lease(self, **data):
        fields = {
            'unit': self.unit.pk, 'tenant': self.tenant.pk, 'start_date': datetime.date.today().isoformat(),
            'monthly_rent': '', 'deposit': '0', 'due_day': '5', 'bill_now': 'on', **data,
        }
        return self.client.post(reverse('rentals:lease_create'), fields)

    def test_create_uses_unit_rent_and_bills_this_month(self):
        resp = self.post_lease()
        lease = Lease.objects.get()
        self.assertRedirects(resp, reverse('rentals:lease_detail', args=[lease.pk]))
        self.assertEqual(lease.monthly_rent, Decimal('12000'))
        self.assertEqual(RentCharge.objects.get().amount, Decimal('12000'))

    def test_occupied_unit_cannot_be_let_twice(self):
        self.post_lease()
        other = Tenant.objects.create(owner=self.alice, full_name='U', phone='0722000000')
        resp = self.post_lease(tenant=other.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Lease.objects.count(), 1)

    def test_cannot_use_another_landlords_unit_or_tenant(self):
        bob_lease = make_lease(self.bob, label='B1')
        bob_lease.is_active = False
        bob_lease.save()
        self.post_lease(unit=bob_lease.unit.pk)
        self.post_lease(tenant=bob_lease.tenant.pk)
        self.assertEqual(Lease.objects.filter(unit__property__owner=self.alice).count(), 0)

    def test_end_lease_frees_unit_and_keeps_history(self):
        self.post_lease()
        lease = Lease.objects.get()
        Payment.objects.create(lease=lease, amount=5000, method='cash')
        resp = self.client.post(reverse('rentals:lease_end', args=[lease.pk]), {'end_date': datetime.date.today()})
        self.assertRedirects(resp, reverse('rentals:lease_detail', args=[lease.pk]))
        lease.refresh_from_db()
        self.assertFalse(lease.is_active)
        self.assertEqual(lease.balance, Decimal('7000'))
        # The unit can be let again.
        self.post_lease()
        self.assertEqual(Lease.objects.filter(is_active=True).count(), 1)

    def test_lease_list_filters(self):
        self.post_lease()
        list_url = reverse('rentals:lease_list')
        self.assertEqual(len(self.client.get(list_url).context['rows']), 1)
        self.assertEqual(len(self.client.get(list_url + '?owing=1').context['rows']), 1)
        self.assertEqual(len(self.client.get(list_url + '?status=ended').context['rows']), 0)

    def test_other_landlord_cannot_edit_or_end(self):
        bob_lease = make_lease(self.bob, label='B1')
        self.assertEqual(self.client.get(reverse('rentals:lease_update', args=[bob_lease.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('rentals:lease_end', args=[bob_lease.pk]),
                                          {'end_date': datetime.date.today()}).status_code, 404)


class LoginRequiredTests(TestCase):
    def test_pages_redirect_anonymous_users(self):
        for name in ['property_list', 'property_create', 'tenant_list', 'tenant_create', 'lease_list', 'lease_create']:
            resp = self.client.get(reverse(f'rentals:{name}'))
            self.assertEqual(resp.status_code, 302, name)
            self.assertTrue(resp['Location'].startswith(reverse('login')), name)


class ThemeSwitchTests(TestCase):
    def test_switch_and_early_theme_script_on_public_and_app_pages(self):
        owner = get_user_model().objects.create_user('landlord')
        lease = make_lease(owner)
        pages = [reverse('rentals:home'), reverse('login'), reverse('accounts:signup'),
                 reverse('rentals:tenant_portal', args=[lease.tenant.portal_token])]
        for url in pages:
            resp = self.client.get(url)
            self.assertContains(resp, 'data-theme-choice="dark"', msg_prefix=url)
            self.assertContains(resp, "localStorage.getItem('theme')", msg_prefix=url)
            self.assertContains(resp, 'js/theme.js', msg_prefix=url)
        self.client.force_login(owner)
        # In the app, the switch lives on the Settings page; every page still applies the saved theme.
        self.assertContains(self.client.get(reverse('accounts:settings')), 'data-theme-choice="auto"')
        self.assertContains(self.client.get(reverse('rentals:dashboard')), "localStorage.getItem('theme')")


class NavigationTests(TestCase):
    def test_signed_in_pages_link_back_to_the_home_page(self):
        owner = get_user_model().objects.create_user('landlord')
        self.client.force_login(owner)
        for url in [reverse('rentals:dashboard'), reverse('rentals:lease_list'), reverse('rentals:lease_create')]:
            self.assertContains(self.client.get(url), f'href="{reverse("rentals:home")}"', msg_prefix=url)
        # The home page itself still offers the way back in.
        self.assertContains(self.client.get(reverse('rentals:home')), reverse('rentals:dashboard'))


class MobileTests(TestCase):
    def test_app_pages_have_a_menu_button_and_table_script(self):
        owner = get_user_model().objects.create_user('landlord')
        self.client.force_login(owner)
        page = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(page, 'data-nav-toggle')
        self.assertContains(page, 'aria-controls="main-nav"')
        self.assertContains(page, 'id="main-nav"')
        self.assertContains(page, 'js/app.js')  # labels table cells so they stack on phones
        self.assertContains(page, 'name="viewport" content="width=device-width, initial-scale=1"')

    def test_hamburger_opens_a_drawer_with_everything_in_it(self):
        owner = get_user_model().objects.create_user('landlord')
        self.client.force_login(owner)
        page = self.client.get(reverse('rentals:dashboard')).content.decode()
        drawer = page.split('id="main-nav"')[1].split('</aside>')[0]
        self.assertIn('Open menu', page)
        self.assertIn('data-nav-close', drawer)  # the X
        self.assertIn('class="nav-backdrop" data-nav-close', page)  # tapping the dimmed page closes it
        for item in ('Tenants', 'Payments', 'Settings', 'Log out'):
            self.assertIn(item, drawer)
        for moved in ('Notifications', 'Help &amp; support', 'theme-switch', 'data-install'):
            self.assertNotIn(moved, drawer)  # these live on the Settings page
        self.assertIn('nav-toggle-dot', page)  # M-Pesa isn't set up yet, so the hamburger shows a dot
        self.assertIn("event.key === 'Escape'", open(finders.find('js/app.js'), encoding='utf-8').read())

    def test_settings_page_gathers_everything(self):
        owner = get_user_model().objects.create_user('landlord', email='l@example.com')
        self.client.force_login(owner)
        page = self.client.get(reverse('accounts:settings'))
        for item in ('M-Pesa', 'Not set up yet', 'Notifications', 'Monthly report card: email', 'Appearance',
                     'theme-switch', 'Install the app', 'Help &amp; support', 'Log out', 'l@example.com'):
            self.assertContains(page, item)
        self.assertNotContains(page, 'Site administration')  # admins only
        # Payment alert controls live here, not on the dashboard.
        self.assertContains(page, 'data-chime-toggle')
        self.assertContains(page, 'data-enable-alerts')
        dashboard = self.client.get(reverse('rentals:dashboard'))
        self.assertNotContains(dashboard, 'data-chime-toggle')
        self.assertNotContains(dashboard, 'data-enable-alerts')
        # The sidebar's one Settings link stays highlighted on the pages it leads to.
        mpesa = self.client.get(reverse('payments:mpesa_settings')).content.decode()
        self.assertIn('href="/settings/" class="active"', mpesa)
