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
            'name': 'Kamau', 'contact': 'kamau@example.com', 'role': 'Accountant', 'level': 'view',
            'password': 'Accounts-2026x'})
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
            'name': 'Kamau', 'contact': 'kamau@example.com', 'role': 'Accountant', 'level': 'view', 'password': ''})  # active unticked
        self.client.logout()
        self.assertFalse(self.client.login(username='kamau@example.com', password='Accounts-2026x'))

    def test_phone_login_and_unique_contacts(self):
        self.client.force_login(self.owner)
        resp = self.client.post(reverse('accounts:viewer_add'), {
            'name': 'Other', 'contact': 'kamau@example.com', 'level': 'view', 'password': 'Another-pass-99'})
        self.assertContains(resp, 'already exists')
        self.client.post(reverse('accounts:viewer_add'), {'name': 'Njeri', 'contact': '0712 000 111',
                                                          'level': 'view', 'password': 'Another-pass-99'})
        self.client.logout()
        self.assertTrue(self.client.login(username='0712000111', password='Another-pass-99'))


class InviteTests(TestCase):
    """The landlord invites someone; they choose their own password from the link."""

    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', first_name='Wanjiku', last_name='Mwangi')
        self.lease = make_lease(self.owner, label='A1')
        self.client.force_login(self.owner)

    def invite(self, **data):
        return self.client.post(reverse('accounts:viewer_add'), {
            'name': 'Otieno', 'contact': 'otieno@example.com', 'role': 'Co-owner', 'level': 'view', 'password': '',
            **data})

    def link(self):
        from django.core import mail

        return next(line for line in mail.outbox[-1].body.splitlines() if '/invite/' in line).strip()

    def test_invite_by_email_then_join(self):
        from django.core import mail

        self.invite()
        access = ViewerAccess.objects.get()
        self.assertFalse(access.joined)
        self.assertIsNotNone(access.invited_at)
        self.assertEqual(mail.outbox[-1].to, ['otieno@example.com'])
        self.assertIn('Wanjiku Mwangi invited you', mail.outbox[-1].subject)
        listing = self.client.get(reverse('accounts:viewers'))
        self.assertContains(listing, 'Invited')
        self.assertContains(listing, '/invite/')  # to copy into WhatsApp

        path = self.link().split('://', 1)[1].split('/', 1)[1]
        self.client.logout()
        page = self.client.get('/' + path)
        self.assertContains(page, 'Wanjiku Mwangi')
        resp = self.client.post('/' + path, {'new_password1': 'Otieno-2026-pw', 'new_password2': 'Otieno-2026-pw'})
        self.assertRedirects(resp, reverse('rentals:dashboard'), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), "You're viewing")
        access.refresh_from_db()
        self.assertTrue(access.joined)
        # One use only.
        self.client.logout()
        self.assertEqual(self.client.get('/' + path).status_code, 400)
        self.assertTrue(self.client.login(username='otieno@example.com', password='Otieno-2026-pw'))

    def test_invite_by_phone_uses_sms(self):
        from notifications.models import SmsMessage

        self.invite(contact='0712 000 222')
        sms = SmsMessage.objects.get()
        self.assertIn('/invite/', sms.body)
        self.assertEqual(sms.phone, '254712000222')

    def test_bad_link(self):
        self.invite()
        access = ViewerAccess.objects.get()
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        uid = urlsafe_base64_encode(force_bytes(access.user.pk))
        self.assertEqual(self.client.get(reverse('accounts:invite', args=[uid, 'nope-nope'])).status_code, 400)
        landlord_uid = urlsafe_base64_encode(force_bytes(self.owner.pk))
        from ..utils import invite_tokens

        self.assertEqual(self.client.get(reverse('accounts:invite', args=[
            landlord_uid, invite_tokens.make_token(self.owner)])).status_code, 400)  # only for invited people

    def test_resend(self):
        from django.core import mail

        self.invite()
        self.client.post(reverse('accounts:viewer_resend', args=[ViewerAccess.objects.get().pk]))
        self.assertEqual(len(mail.outbox), 2)


class EditorTests(TestCase):
    """Someone allowed to make changes can record things, under their own name, but not touch settings."""

    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', first_name='Wanjiku')
        self.lease = make_lease(self.owner, label='A1')
        self.client.force_login(self.owner)
        self.client.post(reverse('accounts:viewer_add'), {
            'name': 'Otieno', 'contact': 'otieno@example.com', 'role': 'Co-owner', 'level': 'edit',
            'password': 'Otieno-2026-pw'})
        self.access = ViewerAccess.objects.get()
        self.client.logout()
        self.client.login(username='otieno@example.com', password='Otieno-2026-pw')

    def test_can_make_changes_recorded_under_their_name(self):
        from audit.models import AuditEntry

        self.assertTrue(self.access.can_edit)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), 'Can make changes')
        self.client.post(reverse('rentals:property_create'), {'name': 'Hillview'})
        prop = Property.objects.get(name='Hillview')
        self.assertEqual(prop.owner, self.owner)  # it's the landlord's account
        self.client.post(reverse('payments:payment_void', args=[
            Payment.objects.create(lease=self.lease, amount=Decimal('500'), method='cash').pk]), {'reason': 'typo'})
        entry = AuditEntry.objects.filter(action__contains='void').first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.access.user)
        self.assertEqual(entry.owner, self.owner)

    def test_settings_stay_the_owners(self):
        for url in (reverse('accounts:settings'), reverse('payments:mpesa_settings'), reverse('accounts:viewers'),
                    reverse('staff:manage')):
            self.assertRedirects(self.client.post(url, {}), reverse('rentals:dashboard'), msg_prefix=url,
                                 fetch_redirect_response=False)

    def test_landlord_can_turn_it_back_to_view_only(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('accounts:viewer_edit', args=[self.access.pk]), {
            'name': 'Otieno', 'contact': 'otieno@example.com', 'role': 'Co-owner', 'level': 'view', 'password': '',
            'active': 'on'})
        self.access.refresh_from_db()
        self.assertFalse(self.access.can_edit)
