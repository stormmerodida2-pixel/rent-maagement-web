import datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEntry
from notifications.models import SmsMessage
from notifications.utils import send_bulk_sms
from rentals.models import Property
from rentals.tests import make_lease

from .models import Announcement, Delivery, PlatformNotice


class AnnouncementTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('landlord', password='pw')
        self.a1 = make_lease(self.owner, label='A1')
        self.a2 = make_lease(self.owner, label='A2')
        other_prop = Property.objects.create(owner=self.owner, name='Riverside')
        self.b1 = make_lease(self.owner, label='B1')
        self.b1.unit.property = other_prop
        self.b1.unit.save()
        self.ended = make_lease(self.owner, label='A3', is_active=False)
        self.stranger = make_lease(User.objects.create_user('bob'), label='Z1')
        self.client.force_login(self.owner)

    def create(self, **data):
        fields = {'title': 'Water off Saturday', 'body': 'No water 9am to 2pm for tank cleaning.',
                  'audience': 'all', 'send_sms': 'on', 'show_on_portal': 'on', **data}
        resp = self.client.post(reverse('announcements:create'), fields)
        return resp, Announcement.objects.order_by('-pk').first()


class AudienceTests(AnnouncementTestCase):
    def test_all_reaches_only_my_active_tenants(self):
        _, a = self.create()
        self.assertEqual({l.pk for l in a.recipient_leases()}, {self.a1.pk, self.a2.pk, self.b1.pk})

    def test_by_property_and_by_unit(self):
        _, a = self.create(audience='properties', properties=[self.b1.unit.property_id])
        self.assertEqual([l.pk for l in a.recipient_leases()], [self.b1.pk])
        _, a = self.create(audience='units', units=[self.a2.unit_id])
        self.assertEqual([l.pk for l in a.recipient_leases()], [self.a2.pk])

    def test_cannot_target_another_landlords_property(self):
        resp, a = self.create(audience='properties', properties=[self.stranger.unit.property_id])
        self.assertEqual(resp.status_code, 200)  # form error, nothing saved
        self.assertIsNone(a)

    def test_needs_a_channel_and_a_selection(self):
        resp, _ = self.create(send_sms='', show_on_portal='')
        self.assertContains(resp, 'Choose at least one way')
        resp, _ = self.create(audience='units')
        self.assertContains(resp, 'Choose at least one unit')


class SendingTests(AnnouncementTestCase):
    def test_draft_preview_then_send(self):
        resp, a = self.create()
        self.assertRedirects(resp, reverse('announcements:detail', args=[a.pk]))
        page = self.client.get(reverse('announcements:detail', args=[a.pk]))
        self.assertContains(page, '3 tenants')
        self.assertContains(page, 'KES 2.40')  # 3 tenants × 1 SMS × 0.80
        self.assertFalse(SmsMessage.objects.exists())  # nothing sent by saving

        self.client.post(reverse('announcements:detail', args=[a.pk]), {'action': 'send'})
        a.refresh_from_db()
        self.assertEqual(a.status, 'sent')
        self.assertEqual(Delivery.objects.filter(announcement=a).count(), 3)
        sms = SmsMessage.objects.filter(kind='announcement')
        self.assertEqual(sms.count(), 3)
        self.assertTrue(all(m.body.startswith('Rentipay: Water off Saturday.') for m in sms))
        self.assertTrue(AuditEntry.objects.filter(action='announcement.sent').exists())

    def test_sending_twice_sends_once(self):
        _, a = self.create()
        url = reverse('announcements:detail', args=[a.pk])
        self.client.post(url, {'action': 'send'})
        self.client.post(url, {'action': 'send'})
        self.assertEqual(SmsMessage.objects.count(), 3)

    def test_portal_only_sends_no_sms(self):
        _, a = self.create(send_sms='')
        self.client.post(reverse('announcements:detail', args=[a.pk]), {'action': 'send'})
        self.assertFalse(SmsMessage.objects.exists())
        self.assertEqual(Delivery.objects.count(), 3)

    def test_scheduled_sends_when_due(self):
        later = timezone.localtime() + datetime.timedelta(hours=2)
        _, a = self.create(scheduled_for=later.strftime('%Y-%m-%dT%H:%M'))
        self.client.post(reverse('announcements:detail', args=[a.pk]), {'action': 'send'})
        a.refresh_from_db()
        self.assertEqual(a.status, 'scheduled')
        call_command('send_announcements', stdout=mock.MagicMock())
        self.assertFalse(Delivery.objects.exists())  # not yet
        Announcement.objects.filter(pk=a.pk).update(scheduled_for=timezone.now() - datetime.timedelta(minutes=1))
        call_command('send_announcements', stdout=mock.MagicMock())
        a.refresh_from_db()
        self.assertEqual((a.status, Delivery.objects.count()), ('sent', 3))

    def test_unschedule_and_delete_draft(self):
        later = timezone.localtime() + datetime.timedelta(hours=2)
        _, a = self.create(scheduled_for=later.strftime('%Y-%m-%dT%H:%M'))
        url = reverse('announcements:detail', args=[a.pk])
        self.client.post(url, {'action': 'send'})
        self.client.post(url, {'action': 'cancel'})
        a.refresh_from_db()
        self.assertEqual(a.status, 'draft')
        self.client.post(url, {'action': 'delete'})
        self.assertFalse(Announcement.objects.filter(pk=a.pk).exists())

    def test_long_message_sms_is_trimmed(self):
        _, a = self.create(body='x' * 900)
        self.assertLessEqual(len(a.sms_text()), Announcement.SMS_LIMIT)
        self.assertTrue(a.sms_text().endswith('Full notice on your rent page.'))
        self.assertEqual(a.sms_parts(), 2)

    def test_other_landlords_cannot_see_or_send(self):
        _, a = self.create()
        self.client.force_login(self.stranger.unit.property.owner)
        url = reverse('announcements:detail', args=[a.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {'action': 'send'}).status_code, 404)


class PortalTests(AnnouncementTestCase):
    def send(self, **data):
        _, a = self.create(**data)
        self.client.post(reverse('announcements:detail', args=[a.pk]), {'action': 'send'})
        a.refresh_from_db()
        return a

    def portal(self, lease):
        self.client.logout()
        return self.client.get(reverse('rentals:tenant_portal', args=[lease.tenant.portal_token]))

    def test_recipient_sees_notice_others_dont(self):
        self.send(audience='units', units=[self.a1.unit_id])
        self.assertContains(self.portal(self.a1), 'Water off Saturday')
        self.assertNotContains(self.portal(self.a2), 'Water off Saturday')
        self.assertNotContains(self.portal(self.stranger), 'Water off Saturday')

    def test_expired_and_removed_notices_disappear(self):
        a = self.send()
        Announcement.objects.filter(pk=a.pk).update(expires_at=timezone.now() - datetime.timedelta(minutes=1))
        self.assertNotContains(self.portal(self.a1), 'Water off Saturday')
        b = self.send(title='Meeting on Sunday')
        self.client.force_login(self.owner)
        self.client.post(reverse('announcements:detail', args=[b.pk]), {'action': 'unpublish'})
        self.assertNotContains(self.portal(self.a1), 'Meeting on Sunday')


@override_settings(SMS={'BACKEND': 'africastalking', 'AT_USERNAME': 'sandbox', 'AT_API_KEY': 'k',
                        'AT_SENDER_ID': '', 'PAYMENT_RECEIPTS': True})
class BulkSmsTests(TestCase):
    @mock.patch('notifications.utils.requests.post')
    def test_one_request_per_100_and_per_number_results(self, post):
        owner = get_user_model().objects.create_user('landlord')
        lease = make_lease(owner)
        numbers = [f'0712{i:06d}' for i in range(150)] + ['bad-number']

        def reply(url, data, **kwargs):
            to = data['to'].split(',')
            recips = [{'number': n, 'statusCode': 101, 'status': 'Success', 'messageId': n} for n in to[:-1]]
            recips.append({'number': to[-1], 'statusCode': 406, 'status': 'UserInBlacklist'})
            response = mock.Mock(status_code=201)
            response.json.return_value = {'SMSMessageData': {'Recipients': recips}}
            return response

        post.side_effect = reply
        msgs = send_bulk_sms([(lease, n) for n in numbers], 'Hello', kind=SmsMessage.Kind.ANNOUNCEMENT)
        self.assertEqual(post.call_count, 2)  # 150 valid numbers -> 2 requests
        self.assertEqual(sum(m.status == 'sent' for m in msgs), 148)
        self.assertEqual(sum(m.error == 'UserInBlacklist' for m in msgs), 2)
        self.assertIn('not a valid', msgs[-1].error)
        self.assertEqual(SmsMessage.objects.count(), 151)


class PlatformNoticeTests(TestCase):
    def test_banner_shows_until_dismissed_or_ended(self):
        user = get_user_model().objects.create_user('landlord')
        self.client.force_login(user)
        notice = PlatformNotice.objects.create(title='Planned maintenance Sunday 2am', level='warning')
        PlatformNotice.objects.create(title='Old news', ends_at=timezone.now() - datetime.timedelta(days=1))
        page = self.client.get(reverse('rentals:dashboard'))
        self.assertContains(page, 'Planned maintenance Sunday 2am')
        self.assertNotContains(page, 'Old news')
        self.client.post(reverse('announcements:dismiss_notice', args=[notice.pk]), {'next': '/dashboard/'})
        self.assertNotContains(self.client.get(reverse('rentals:dashboard')), 'Planned maintenance')
