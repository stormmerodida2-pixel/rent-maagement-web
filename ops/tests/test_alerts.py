"""Errors inside the app reach the site owner: a phone push and an email, at most once an hour per problem."""

import logging
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings

from ops.utils import Job, run_job

NTFY = 'https://ntfy.sh/test-topic'


@override_settings(ALERT_NTFY_URL=NTFY, ALERT_EMAIL='owner@example.com')
class ErrorAlertTests(TestCase):
    def setUp(self):
        cache.clear()

    def crash(self, path='/payments/'):
        request = RequestFactory().get(path)
        try:
            raise ZeroDivisionError("division by zero")
        except ZeroDivisionError:
            logging.getLogger('django.request').error('Internal Server Error: %s', path, exc_info=True,
                                                     extra={'request': request, 'status_code': 500})

    @mock.patch('requests.post')
    def test_a_crash_alerts_once_an_hour(self, post):
        self.crash()
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], NTFY)
        self.assertIn(b'ZeroDivisionError at /payments/', post.call_args.kwargs['data'])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('ZeroDivisionError at /payments/', mail.outbox[0].subject)
        self.assertIn('Traceback', mail.outbox[0].body)

        self.crash()  # same problem: quiet
        self.assertEqual(post.call_count, 1)
        self.crash('/reports/')  # somewhere else: a new alert
        self.assertEqual(post.call_count, 2)

    @mock.patch('requests.post')
    def test_failed_jobs_alert(self, post):
        with mock.patch('ops.utils.call_command', side_effect=RuntimeError('boom')):
            self.assertFalse(run_job(Job('send_rent_reminders', 'send_rent_reminders', daily_at=None)))
        self.assertIn(b'RuntimeError', post.call_args.kwargs['data'])
        self.assertIn('Scheduled job send_rent_reminders failed', mail.outbox[0].body)

    @mock.patch('requests.post')
    def test_bad_host_noise_is_ignored(self, post):
        logging.getLogger('django.security.DisallowedHost').error('Invalid HTTP_HOST header')
        post.assert_not_called()

    @mock.patch('requests.post', side_effect=OSError('ntfy down'))
    def test_alert_problems_never_break_anything(self, post):
        self.crash()  # no exception escapes
        self.assertEqual(len(mail.outbox), 0)  # ntfy failed first; best effort


class NoAlertsConfiguredTests(TestCase):
    @mock.patch('requests.post')
    def test_quiet_without_settings(self, post):
        logging.getLogger('payments').error('Something failed')
        post.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)
