from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from notifications.models import SmsMessage
from rentals.tests import make_lease

from .models import Complaint, SupportMessage


class ComplaintFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user('landlord', password='pw', email='landlord@example.com')
        self.lease = make_lease(self.owner)
        self.tenant = self.lease.tenant
        self.token = self.tenant.portal_token

    def send(self, **data):
        return self.client.post(reverse('support:tenant_messages', args=[self.token]), {
            'category': 'neighbours', 'subject': 'Loud music at night', 'message': 'Every night after 11pm.', **data,
        })

    def test_tenant_to_landlord_and_back(self):
        resp = self.send()
        complaint = Complaint.objects.get()
        self.assertRedirects(resp, reverse('support:tenant_complaint', args=[self.token, complaint.pk]))
        self.assertEqual((complaint.owner, complaint.lease, complaint.status), (self.owner, self.lease, 'open'))
        self.assertEqual(mail.outbox[0].to, ['landlord@example.com'])
        self.assertIn('Every night after 11pm.', mail.outbox[0].body)

        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('rentals:dashboard')), '<span class="count">1</span>', html=False)
        detail = reverse('support:complaint_detail', args=[complaint.pk])
        self.assertContains(self.client.get(detail), 'Every night after 11pm.')
        complaint.refresh_from_db()
        self.assertFalse(complaint.unread_by_landlord)

        self.client.post(detail, {'body': "I've spoken to B2.", 'status': 'in_progress'})
        complaint.refresh_from_db()
        self.assertEqual(complaint.status, 'in_progress')
        sms = SmsMessage.objects.get()
        self.assertIn('Your landlord replied', sms.body)
        self.assertIn(f'/t/{self.token}/messages/{complaint.pk}/', sms.body)

        self.client.logout()
        page = self.client.get(reverse('support:tenant_complaint', args=[self.token, complaint.pk]))
        self.assertContains(page, "I&#x27;ve spoken to B2.")

    def test_tenant_reply_reopens_resolved(self):
        self.send()
        complaint = Complaint.objects.get()
        complaint.set_status('resolved')
        self.client.post(reverse('support:tenant_complaint', args=[self.token, complaint.pk]), {'body': 'Still happening'})
        complaint.refresh_from_db()
        self.assertEqual((complaint.status, complaint.unread_by_landlord), ('open', True))

    def test_landlord_must_reply_or_change_status(self):
        self.send()
        complaint = Complaint.objects.get()
        self.client.force_login(self.owner)
        resp = self.client.post(reverse('support:complaint_detail', args=[complaint.pk]), {'body': '', 'status': 'open'})
        self.assertContains(resp, 'Write a reply or change the status')

    def test_scoping(self):
        self.send()
        complaint = Complaint.objects.get()
        other = make_lease(get_user_model().objects.create_user('bob'), label='B1')
        # Another tenant's link can't open this conversation; another landlord can't either.
        self.assertEqual(self.client.get(
            reverse('support:tenant_complaint', args=[other.tenant.portal_token, complaint.pk])).status_code, 404)
        self.client.force_login(other.unit.property.owner)
        self.assertEqual(self.client.get(reverse('support:complaint_detail', args=[complaint.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse('support:complaint_list')), 'Loud music')

    def test_tenant_messages_are_rate_limited(self):
        for i in range(20):
            self.send(subject=f'S{i}')
        self.send(subject='one too many')
        self.assertFalse(Complaint.objects.filter(subject='one too many').exists())


@override_settings(SUPPORT_EMAIL='help@rent.example')
class ContactTests(TestCase):
    def setUp(self):
        cache.clear()

    def post(self, **data):
        return self.client.post(reverse('support:contact'), {
            'name': 'Jane', 'email': 'jane@example.com', 'topic': 'question', 'message': 'Do you support Tills?', **data,
        })

    def test_public_contact_is_saved_and_emailed(self):
        self.assertRedirects(self.post(), reverse('support:contact'))
        msg = SupportMessage.objects.get()
        self.assertIsNone(msg.user)
        self.assertEqual(mail.outbox[0].to, ['help@rent.example'])
        self.assertEqual(mail.outbox[0].reply_to, ['jane@example.com'])

    def test_logged_in_landlord_is_linked_and_prefilled(self):
        user = get_user_model().objects.create_user('grace', email='grace@example.com', first_name='Grace')
        self.client.force_login(user)
        page = self.client.get(reverse('support:contact'))
        self.assertContains(page, 'value="grace@example.com"')
        self.assertContains(page, 'Help &amp; support')
        self.post(email='grace@example.com')
        self.assertEqual(SupportMessage.objects.get().user, user)

    def test_honeypot_drops_bots_quietly(self):
        resp = self.post(website='http://spam.example')
        self.assertRedirects(resp, reverse('support:contact'))
        self.assertFalse(SupportMessage.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_rate_limited(self):
        for _ in range(5):
            self.post()
        self.assertEqual(self.post().status_code, 429)
        self.assertEqual(SupportMessage.objects.count(), 5)
