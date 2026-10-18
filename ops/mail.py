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
