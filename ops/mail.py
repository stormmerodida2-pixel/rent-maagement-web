"""Emails printed to the terminal when no email server is set (your computer): readable, with every link on one
line you can click. Django's own console backend prints the encoded email, which splits long links with "="."""

from django.core.mail.backends.console import EmailBackend


class ReadableConsoleBackend(EmailBackend):
    def write_message(self, message):
        lines = [f'From: {message.from_email}', f'To: {", ".join(message.to)}']
        if message.cc:
            lines.append(f'Cc: {", ".join(message.cc)}')
        lines += [f'Subject: {message.subject}', '', message.body.rstrip()]
        if getattr(message, 'alternatives', None):
            lines += ['', f'(Also sent as {", ".join(alt[1] for alt in message.alternatives)}.)']
        if message.attachments:
            lines += [f'(Attachments: {", ".join(str(a[0]) for a in message.attachments)}.)']
        self.stream.write('\n'.join(lines) + '\n' + '-' * 79 + '\n')


from django.core.mail.backends.smtp import EmailBackend as SMTPBackend  # noqa: E402


class BrandedSMTPBackend(SMTPBackend):
    """The normal SMTP sender, giving every plain-text email its designed HTML version first (ops/email_design)."""

    def send_messages(self, email_messages):
        from .email_design import add_html

        for message in email_messages:
            try:
                add_html(message)
            except Exception:  # the plain email still goes if the design can't be made
                import logging

                logging.getLogger(__name__).exception('Could not add the HTML version to "%s"', message.subject)
        return super().send_messages(email_messages)
