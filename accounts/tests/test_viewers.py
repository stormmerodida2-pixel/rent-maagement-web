"""Read-only access: a co-owner or accountant sees the landlord's account but can't change anything."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from payments.models import Payment
from rentals.models import Property
from rentals.tests import make_lease

from ..models import ViewerAccess


class ViewerTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', password='owner-pass-123', first_name='Wanjiku')
        self.lease = make_lease(self.owner, label='A1')
        Payment.objects.create(lease=self.lease, amount=Decimal('15000'), method='cash')
        self.client.force_login(self.owner)
        self.client.post(reverse('accounts:viewer_add'), {
            'name': 'Kamau', 'contact': 'kamau@example.com', 'role': 'Accountant', 'password': 'Accounts-2026x'})
        self.access = ViewerAccess.objects.get()
        self.client.logout()
        self.client.login(username='kamau@example.com', password='Accounts-2026x')

    def test_sees_the_landlords_account(self):
        dashboard = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(dashboard, "You're viewing")
        self.assertContains(dashboard, 'Wanjiku')
        self.assertContains(dashboard, 'Read-only · Accountant')
        self.assertEqual(self.client.get(reverse('rentals:lease_detail', args=[self.lease.pk])).status_code, 200)
        self.assertContains(self.client.get(reverse('payments:payment_list')), 'KES 15,000')
        self.assertEqual(self.client.get(reverse('reports:payments_csv')).status_code, 200)  # downloads are fine

    def test_cannot_change_anything(self):
        resp = self.client.post(reverse('rentals:property_create'), {'name': 'Sneaky'}, follow=True)
        self.assertContains(resp, "read-only access")
        self.assertFalse(Property.objects.filter(name='Sneaky').exists())
        self.client.post(reverse('payments:payment_void', args=[Payment.objects.get().pk]), {'reason': 'x'})
        self.assertEqual(Payment.objects.count(), 1)

    def test_settings_are_the_owners(self):
        for url in (reverse('accounts:settings'), reverse('payments:mpesa_settings'), reverse('accounts:viewers'),
                    reverse('staff:manage')):
            resp = self.client.get(url)
            self.assertRedirects(resp, reverse('rentals:dashboard'), msg_prefix=url, fetch_redirect_response=False)

    def test_own_password_is_theirs(self):
        resp = self.client.post(reverse('accounts:password_change'), {
            'old_password': 'Accounts-2026x', 'new_password1': 'New-accounts-77', 'new_password2': 'New-accounts-77'})
        self.assertRedirects(resp, reverse('rentals:dashboard'), fetch_redirect_response=False)
        self.access.user.refresh_from_db()
        self.assertTrue(self.access.user.check_password('New-accounts-77'))
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password('owner-pass-123'))  # the landlord's is untouched

    def test_landlord_switches_it_off(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('accounts:viewer_edit', args=[self.access.pk]), {
            'name': 'Kamau', 'contact': 'kamau@example.com', 'role': 'Accountant', 'password': ''})  # active unticked
        self.client.logout()
        self.assertFalse(self.client.login(username='kamau@example.com', password='Accounts-2026x'))

    def test_phone_login_and_unique_contacts(self):
        self.client.force_login(self.owner)
        resp = self.client.post(reverse('accounts:viewer_add'), {
            'name': 'Other', 'contact': 'kamau@example.com', 'password': 'Another-pass-99'})
        self.assertContains(resp, 'already exists')
        self.client.post(reverse('accounts:viewer_add'), {'name': 'Njeri', 'contact': '0712 000 111',
                                                          'password': 'Another-pass-99'})
        self.client.logout()
        self.assertTrue(self.client.login(username='0712000111', password='Another-pass-99'))
