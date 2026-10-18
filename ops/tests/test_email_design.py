"""Every plain-text email also goes as a branded HTML version: logo, card, a button for the main link, footer."""

from unittest import mock

from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.test import TestCase, override_settings

from ops.email_design import add_html, blocks_from_text, button_label

BODY = ('Hi Grace,\n\nSomeone asked to reset your password <script>.\nIf that was you:\n\n'
        'https://rentipay.co.ke/password-reset/MjE/abc/\n\nPlease also:\n- record cash\n- upload a statement\n\n'
        'Rentipay')


@override_settings(SITE_URL='https://rentipay.co.ke')
class EmailDesignTests(TestCase):
    def html_of(self, message):
        add_html(message)
        return message.alternatives[0][0]

    def test_branded_html_with_a_button(self):
        message = EmailMessage('Reset your password', BODY, 'a@x.com', ['g@x.com'])
        html = self.html_of(message)
        self.assertIn('logo-transparent', html)
        self.assertIn('>Choose a new password</a>', html)
        self.assertIn('href="https://rentipay.co.ke/password-reset/MjE/abc/"', html)
        self.assertIn('>record cash</td>', html)
        self.assertIn('Hi Grace,', html)
        self.assertIn('&lt;script&gt;', html)  # text is escaped
        self.assertNotIn('<script>', html)
        self.assertEqual(message.body, BODY)  # the plain version still goes

    def test_button_words_follow_the_link(self):
        self.assertEqual(button_label('https://x/verify-email/abc/'), 'Confirm my email')
        self.assertEqual(button_label('https://x/invite/MQ/abc/'), 'Accept the invite')
        self.assertEqual(button_label('https://x/somewhere/'), 'Open Rentipay')

    def test_sign_off_moves_to_the_footer(self):
        kinds = [b['kind'] for b in blocks_from_text(BODY)]
        self.assertEqual(kinds, ['greeting', 'text', 'button', 'text', 'list'])

    def test_emails_with_their_own_design_and_alerts_are_left_alone(self):
        designed = EmailMultiAlternatives('Report', 'text', to=['g@x.com'])
        designed.attach_alternative('<p>mine</p>', 'text/html')
        add_html(designed)
        self.assertEqual(designed.alternatives[0][0], '<p>mine</p>')
        alert = EmailMessage('[Rentipay error] KeyError', 'Traceback (most recent call last):\n  ...', to=['me@x.com'])
        add_html(alert)
        self.assertFalse(getattr(alert, 'alternatives', None))

    @override_settings(MAILERS={'default': {'BACKEND': 'ops.mail.BrandedSMTPBackend',
                                            'OPTIONS': {'host': 'localhost', 'port': 25}}})
    def test_the_smtp_sender_adds_it(self):
        from django.core.mail import get_connection

        message = EmailMessage('Confirm your email', 'Hi Grace,\n\nhttps://x/verify-email/abc/', to=['g@x.com'])
        with mock.patch('django.core.mail.backends.smtp.EmailBackend.send_messages', return_value=1) as send:
            get_connection().send_messages([message])
        sent = send.call_args.args[0][0]
        self.assertIn('Confirm my email', sent.alternatives[0][0])
