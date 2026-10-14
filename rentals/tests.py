import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from maintenance.models import MaintenanceRequest
from payments.models import Payment

from .models import Lease, Property, RentCharge, Tenant, Unit


def make_lease(owner, label='A1', rent='15000', start=datetime.date(2026, 1, 15), **kwargs):
    prop, _ = Property.objects.get_or_create(owner=owner, name=f'{owner.username} Apartments')
    unit = Unit.objects.create(property=prop, label=label, default_rent=rent)
    tenant = Tenant.objects.create(owner=owner, full_name=f'Tenant {label}', phone='0712345678')
    return Lease.objects.create(unit=unit, tenant=tenant, start_date=start, monthly_rent=rent, **kwargs)


class LeaseTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.lease = make_lease(self.owner, due_day=5)

    def test_ensure_charge_is_idempotent_and_uses_due_day(self):
        charge, created = self.lease.ensure_charge_for(datetime.date(2026, 3, 20))
        self.assertTrue(created)
        self.assertEqual(charge.period, datetime.date(2026, 3, 1))
        self.assertEqual(charge.due_date, datetime.date(2026, 3, 5))
        _, created_again = self.lease.ensure_charge_for(datetime.date(2026, 3, 1))
        self.assertFalse(created_again)

    def test_no_charge_before_start_or_after_end(self):
        self.assertEqual(self.lease.ensure_charge_for(datetime.date(2025, 12, 1)), (None, False))
        self.lease.end_date = datetime.date(2026, 4, 30)
        self.assertEqual(self.lease.ensure_charge_for(datetime.date(2026, 5, 1)), (None, False))
        # The start month itself is billed even though the lease began mid-month.
        self.assertTrue(self.lease.ensure_charge_for(datetime.date(2026, 1, 1))[1])

    def test_balance(self):
        self.lease.ensure_charge_for(datetime.date(2026, 1, 1))
        self.lease.ensure_charge_for(datetime.date(2026, 2, 1))
        Payment.objects.create(lease=self.lease, amount=Decimal('20000'), method='cash')
        self.assertEqual(self.lease.balance, Decimal('10000'))

    def test_generate_rent_charges_command(self):
        make_lease(self.owner, label='A2', is_active=False)
        call_command('generate_rent_charges', month='2026-02', stdout=open('/dev/null', 'w'))
        self.assertEqual(RentCharge.objects.count(), 1)
        self.assertEqual(RentCharge.objects.get().lease, self.lease)


class ViewScopingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user('alice', password='pw')
        self.bob = User.objects.create_user('bob', password='pw')
        self.alice_lease = make_lease(self.alice)
        self.bob_lease = make_lease(self.bob, label='B1')

    def test_dashboard_requires_login(self):
        resp = self.client.get(reverse('rentals:dashboard'))
        self.assertEqual(resp.status_code, 302)

    def test_landlord_cannot_see_other_landlords_lease(self):
        self.client.login(username='alice', password='pw')
        self.assertEqual(self.client.get(reverse('rentals:lease_detail', args=[self.alice_lease.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('rentals:lease_detail', args=[self.bob_lease.pk])).status_code, 404)

    def test_dashboard_shows_only_own_arrears(self):
        self.alice_lease.ensure_charge_for(datetime.date(2026, 1, 1))
        self.bob_lease.ensure_charge_for(datetime.date(2026, 1, 1))
        self.client.login(username='alice', password='pw')
        resp = self.client.get(reverse('rentals:dashboard'))
        self.assertEqual([lease for lease, _ in resp.context['arrears']], [self.alice_lease])


class TenantTests(TestCase):
    def test_phone_is_normalized_and_validated(self):
        owner = get_user_model().objects.create_user('landlord')
        tenant = Tenant.objects.create(owner=owner, full_name='A', phone='+254 712-345-678')
        self.assertEqual(tenant.phone, '254712345678')
        tenant.phone = '12345'
        with self.assertRaises(ValidationError):
            tenant.full_clean()

    def test_find_by_account_number(self):
        owner = get_user_model().objects.create_user('landlord')
        lease = make_lease(owner)
        for ref in [lease.account_number, f'lease {lease.pk}', f'Lease-0{lease.pk}']:
            self.assertEqual(Lease.find_by_account_number(ref), lease, ref)
        for ref in ['', None, 'LEASE', 'house A1', 'LEASE999999', str(lease.pk)]:
            self.assertIsNone(Lease.find_by_account_number(ref), ref)


@override_settings(MPESA={'CALLBACK_BASE_URL': 'https://x.test'})
class TenantPortalTests(TestCase):
    def setUp(self):
        owner = get_user_model().objects.create_user('landlord')
        make_account(owner)
        self.lease = make_lease(owner)
        self.lease.ensure_charge_for(datetime.date(2026, 1, 1))
        self.tenant = self.lease.tenant
        self.url = reverse('rentals:tenant_portal', args=[self.tenant.portal_token])

    def test_shows_balance_without_login(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, 'KES 15,000')
        self.assertContains(resp, self.lease.account_number)

    def test_hides_payment_options_until_landlord_connects_mpesa(self):
        self.lease.unit.property.owner.mpesa_account.delete()
        resp = self.client.get(self.url)
        self.assertContains(resp, "Online payment isn't available yet")
        self.assertNotContains(resp, 'Pay with M-Pesa')

    def test_bad_token_is_404(self):
        self.assertEqual(self.client.get(reverse('rentals:tenant_portal', args=['nope'])).status_code, 404)

    def test_submit_maintenance_request(self):
        resp = self.client.post(self.url, {
            'action': 'maintenance', 'unit': self.lease.unit.pk, 'title': 'Broken window', 'priority': 'high',
        })
        self.assertEqual(resp.status_code, 302)
        req = MaintenanceRequest.objects.get()
        self.assertEqual((req.unit, req.reported_by, req.title), (self.lease.unit, self.tenant, 'Broken window'))

    def test_cannot_file_request_for_someone_elses_unit(self):
        other = make_lease(get_user_model().objects.create_user('other'), label='Z9')
        self.client.post(self.url, {'action': 'maintenance', 'unit': other.unit.pk, 'title': 'x', 'priority': 'low'})
        self.assertFalse(MaintenanceRequest.objects.exists())

    @mock.patch('payments.mpesa.stk_push')
    def test_pay_always_uses_tenants_own_phone(self, stk_push):
        stk_push.return_value = mock.Mock(phone='254712345678', amount=500)
        self.client.post(self.url, {'action': 'pay', 'lease': self.lease.pk, 'amount': '500', 'phone': '0799999999'})
        stk_push.assert_called_once_with(self.lease, Decimal('500'))

    @mock.patch('payments.mpesa.stk_push')
    def test_cannot_pay_against_another_tenants_lease(self, stk_push):
        other = make_lease(get_user_model().objects.create_user('other'), label='Z9')
        self.client.post(self.url, {'action': 'pay', 'lease': other.pk, 'amount': '500'})
        stk_push.assert_not_called()


def make_account(owner, **kwargs):
    from payments.tests import make_account

    return make_account(owner, **kwargs)


class PublicPagesTests(TestCase):
    def test_home_page_is_public(self):
        resp = self.client.get(reverse('rentals:home'))
        self.assertContains(resp, 'Collect rent on')
        self.assertContains(resp, 'id="features"')
        self.assertContains(resp, reverse('login'))

    def test_home_shows_dashboard_link_when_logged_in(self):
        user = get_user_model().objects.create_user('landlord', password='pw')
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse('rentals:home')), reverse('rentals:dashboard'))

    def test_login_redirects_to_dashboard(self):
        get_user_model().objects.create_user('landlord', password='pw')
        resp = self.client.post(reverse('login'), {'username': 'landlord', 'password': 'pw'})
        self.assertRedirects(resp, reverse('rentals:dashboard'))

    def test_dashboard_redirects_anonymous_to_login(self):
        resp = self.client.get(reverse('rentals:dashboard'))
        self.assertRedirects(resp, f"{reverse('login')}?next={reverse('rentals:dashboard')}")


class StatementTests(TestCase):
    def test_running_balance_newest_first(self):
        from .views import statement

        lease = make_lease(get_user_model().objects.create_user('landlord'))
        lease.ensure_charge_for(datetime.date(2026, 1, 1))
        lease.ensure_charge_for(datetime.date(2026, 2, 1))
        Payment.objects.create(lease=lease, amount=Decimal('20000'), method='cash',
                               paid_at=datetime.datetime(2026, 1, 20, 10, tzinfo=datetime.UTC))
        rows = statement(lease)
        self.assertEqual([r['kind'] for r in rows], ['charge', 'payment', 'charge'])
        self.assertEqual([r['balance'] for r in rows], [Decimal('10000'), Decimal('-5000'), Decimal('15000')])

    def test_dashboard_and_lease_page_render(self):
        user = get_user_model().objects.create_user('landlord', password='pw')
        lease = make_lease(user)
        lease.ensure_charge_for(datetime.date.today())
        MaintenanceRequest.objects.create(unit=lease.unit, title='Leak', priority='urgent')
        self.client.force_login(user)
        dash = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(dash, 'Finish setting up')
        self.assertContains(dash, 'Next: Connect M-Pesa')
        account = make_account(user)
        account.verified_at = account.created_at
        account.save()
        dash = self.client.get(reverse('rentals:dashboard'))
        self.assertNotContains(dash, 'Finish setting up')
        self.assertNotContains(dash, 'Connect M-Pesa to start')
        self.assertContains(dash, 'KES 15,000')
        self.assertContains(dash, '1 high priority')
        self.assertEqual(dash.context['collection_rate'], 0)
        page = self.client.get(reverse('rentals:lease_detail', args=[lease.pk]))
        start, end = lease.cycle_start(datetime.date.today()), lease.cycle_end(datetime.date.today())
        self.assertContains(page, f'Rent {start.day} {start:%b} – {end.day} {end:%b %Y}')
        self.assertContains(page, lease.account_number)
