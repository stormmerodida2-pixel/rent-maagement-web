"""Settings → SMS: the Africa's Talking account is set in the app, with a connection check and a test SMS."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import SmsMessage, SmsSettings
from ..utils import send_sms, sms_config

SERVER = {'BACKEND': 'console', 'AT_USERNAME': 'sandbox', 'AT_API_KEY': 'server-key', 'AT_SENDER_ID': '',
          'PAYMENT_RECEIPTS': True}


@override_settings(SMS=SERVER)
class SmsSettingsTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('admin', 'admin@example.com', 'pw')
        self.url = reverse('notifications:sms_settings')
        self.client.force_login(self.admin)

    def save(self, **data):
        return self.client.post(self.url, {'live': 'on', 'username': 'stormer', 'api_key': 'atsk_new',
                                           'sender_id': '', **data})

    def test_server_settings_until_saved(self):
        self.assertEqual(sms_config()['AT_API_KEY'], 'server-key')
        self.assertContains(self.client.get(self.url), "using the server's settings")

    def test_save_in_the_app(self):
        self.save()
        cfg = sms_config()
        self.assertEqual((cfg['BACKEND'], cfg['AT_USERNAME'], cfg['AT_API_KEY']), ('africastalking', 'stormer', 'atsk_new'))
        page = self.client.get(self.url)
        self.assertContains(page, 'Sending real SMS')
        self.assertNotContains(page, 'atsk_new')  # never shown again

    def test_blank_key_keeps_the_saved_one_or_the_servers(self):
        self.save(api_key='')  # first time: the key already on the server
        self.assertEqual(SmsSettings.load().api_key, 'server-key')
        self.save(api_key='atsk_second')
        self.save(api_key='')
        self.assertEqual(SmsSettings.load().api_key, 'atsk_second')

    def test_live_needs_username_and_key(self):
        with override_settings(SMS={**SERVER, 'AT_API_KEY': ''}):
            resp = self.save(username='', api_key='')
        self.assertContains(resp, 'enter the username and API key')

    @mock.patch('notifications.utils.requests')
    def test_check_connection_shows_the_balance_or_the_problem(self, req):
        self.save()
        req.get.return_value.status_code = 200
        req.get.return_value.headers = {'content-type': 'application/json'}
        req.get.return_value.json.return_value = {'UserData': {'balance': 'KES 120.0000'}}
        self.assertContains(self.client.post(self.url, {'action': 'check'}, follow=True), 'Balance: KES 120.0000')
        self.assertEqual(req.get.call_args.kwargs['headers']['apiKey'], 'atsk_new')
        req.get.return_value.status_code = 401
        req.get.return_value.json.return_value = {'errorMessage': 'The supplied authentication is invalid'}
        self.assertContains(self.client.post(self.url, {'action': 'check'}, follow=True),
                            'The supplied authentication is invalid')

    @mock.patch('notifications.utils.requests.post')
    def test_sending_uses_the_saved_account(self, post):
        self.save()
        post.return_value.json.return_value = {'SMSMessageData': {'Recipients': [
            {'statusCode': 101, 'messageId': 'ATX1', 'cost': 'KES 0.8000', 'number': '+254712345678'}]}}
        msg = send_sms('0712345678', 'Hello')
        self.assertEqual(msg.status, 'sent')
        self.assertEqual(post.call_args.kwargs['data']['username'], 'stormer')
        self.assertEqual(post.call_args.kwargs['headers']['apiKey'], 'atsk_new')
        resp = self.client.post(self.url, {'action': 'test', 'test_phone': '0712345678'}, follow=True)
        self.assertContains(resp, 'Sent to 254712345678')

    def test_not_live_only_logs(self):
        self.save(live='')
        self.assertEqual(send_sms('0712345678', 'Hello').status, SmsMessage.Status.LOGGED)

    def test_landlords_cannot_see_it(self):
        self.client.force_login(get_user_model().objects.create_user('landlord'))
        self.assertEqual(self.client.get(self.url).status_code, 404)


class CheckConnectionTests(TestCase):
    """New Africa's Talking keys may not read the balance; the check then asks the SMS service (sending nothing)."""

    CFG = {'AT_USERNAME': 'stormer', 'AT_API_KEY': 'atsk_test'}

    def response(self, status, text, json_data=None):
        from unittest import mock

        resp = mock.Mock(status_code=status, text=text)
        resp.headers = {'content-type': 'application/json' if json_data is not None else 'text/plain'}
        resp.json.return_value = json_data or {}
        return resp

    def test_balance_works(self):
        from unittest import mock

        from ..utils import check_sms_account

        with mock.patch('requests.get', return_value=self.response(200, '', {'UserData': {'balance': 'KES 10'}})):
            self.assertEqual(check_sms_account(self.CFG), (True, 'Connected. Balance: KES 10.'))

    def test_new_key_without_balance_access(self):
        from unittest import mock

        from ..utils import check_sms_account

        denied = self.response(401, '', {'errorMessage': 'The supplied authentication is invalid'})
        accepted = self.response(400, 'requirement failed: Please provide the phone numbers to send to')
        with mock.patch('requests.get', return_value=denied), mock.patch('requests.post', return_value=accepted) as post:
            ok, text = check_sms_account(self.CFG)
        self.assertTrue(ok)
        self.assertIn('accepted the username and key', text)
        self.assertEqual(post.call_args.kwargs['data']['to'], '')  # nothing is sent

    def test_wrong_key(self):
        from unittest import mock

        from ..utils import check_sms_account

        denied = self.response(401, '', {'errorMessage': 'The supplied authentication is invalid'})
        rejected = self.response(401, 'The supplied authentication is invalid')
        with mock.patch('requests.get', return_value=denied), mock.patch('requests.post', return_value=rejected):
            self.assertEqual(check_sms_account(self.CFG),
                             (False, "Africa's Talking said: The supplied authentication is invalid"))
