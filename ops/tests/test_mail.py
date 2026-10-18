import io

from django.core.mail import EmailMessage
from django.test import SimpleTestCase

from ops.mail import ReadableConsoleBackend


class ReadableConsoleBackendTests(SimpleTestCase):
    def test_long_links_stay_on_one_line(self):
        link = 'http://localhost:8000/verify-email/' + 'x' * 120 + '/'
        out = io.StringIO()
        ReadableConsoleBackend(stream=out).send_messages([
            EmailMessage('Confirm your email', f'Open this link:\n\n{link}\n\nThanks — Rentipay', 'a@example.com',
                         ['b@example.com'])])
        text = out.getvalue()
        self.assertIn(link + '\n', text)
        self.assertIn('Subject: Confirm your email', text)
        self.assertIn('Thanks — Rentipay', text)  # not encoded
