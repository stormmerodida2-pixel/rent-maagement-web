"""Every email in Rentipay's design: the plain-text message is also sent as a branded HTML version (logo, card,
a button for the main link, footer). Emails that already have an HTML version, and technical alerts, are left as
they are. The text version always goes too, for simple email apps."""

import re

from django.conf import settings
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.utils.html import escape, mark_safe, urlize

URL = re.compile(r'^https?://\S+$')
# The button's words, from where the link goes.
BUTTONS = [
    ('/verify-email/', 'Confirm my email'),
    ('/password-reset/', 'Choose a new password'),
    ('/invite/', 'Accept the invite'),
    ('/reports/tax', 'See the workings'),
    ('/reports/', 'See the report'),
    ('/complaints/', 'Read and reply'),
    ('/vacancies/', 'See the application'),
    ('/billing/', 'See my bill'),
    ('/payments/', 'Review it'),
    ('/leases/', 'Open the tenant'),
    ('/t/', 'Open my rent page'),
]


def button_label(url):
    path = re.sub(r'^https?://[^/]+', '', url)
    return next((label for prefix, label in BUTTONS if prefix in path), f'Open {settings.APP_NAME}')


BULLETS = ('- ', '• ')
LINK_STYLE = 'style="color:#0e7a4b"'


def _inline(text):
    """One line of text as safe HTML, its links and email addresses clickable and in Rentipay green."""
    return mark_safe(urlize(escape(text), autoescape=False).replace('<a ', f'<a {LINK_STYLE} '))


def blocks_from_text(body):
    """The text split into what the template draws: greeting, paragraphs, bullet lists and link buttons."""
    blocks, first_button = [], True
    paragraphs = [p.strip('\n') for p in re.split(r'\n\s*\n', body.strip()) if p.strip()]
    if paragraphs and paragraphs[-1].strip() == settings.APP_NAME:
        paragraphs = paragraphs[:-1]  # the sign-off: the footer says it
    for i, para in enumerate(paragraphs):
        lines = [line.rstrip() for line in para.split('\n')]
        if i == 0 and len(lines) == 1 and re.match(r'^(Hi|Hello|Habari)\b.*,$', lines[0]):
            blocks.append({'kind': 'greeting', 'text': lines[0]})
        elif len(lines) == 1 and URL.match(lines[0].strip()):
            url = lines[0].strip()
            if first_button:
                blocks.append({'kind': 'button', 'url': url, 'label': button_label(url)})
                first_button = False
            else:
                blocks.append({'kind': 'text', 'html': _inline(url)})
        elif all(line.startswith(BULLETS) for line in lines):
            blocks.append({'kind': 'list', 'items': [_inline(line[2:]) for line in lines]})
        elif len(lines) > 1 and all(line.startswith(BULLETS) for line in lines[1:]):
            # "Please log in and:" followed by bullets.
            blocks.append({'kind': 'text', 'html': _inline(lines[0])})
            blocks.append({'kind': 'list', 'items': [_inline(line[2:]) for line in lines[1:]]})
        else:
            blocks.append({'kind': 'text', 'html': mark_safe('<br>'.join(_inline(line) for line in lines))})
    return blocks


def render_html(subject, body):
    from ops.models import SiteSettings

    site = settings.SITE_URL.rstrip('/')
    try:
        address = SiteSettings.load().legal().get('ADDRESS', '')
    except Exception:  # an email must still go out even if the settings can't be read
        address = ''
    blocks = blocks_from_text(body)
    first_text = next((b for b in blocks if b['kind'] == 'text'), None)
    preheader = re.sub(r'<[^>]+>', ' ', str(first_text['html']))[:140] if first_text else subject
    # The subject, without a leading "Rentipay: ", is the heading.
    title = re.sub(rf'^{re.escape(settings.APP_NAME)}:\s*', '', subject).strip()
    title = title[:1].upper() + title[1:]
    return render_to_string('emails/base.html', {
        'subject': subject, 'title': title, 'blocks': blocks, 'site_url': site, 'logo_url': site + static('img/logo-transparent.png'),
        'APP_NAME': settings.APP_NAME, 'address': ' '.join(address.split()), 'preheader': preheader,
    })


def add_html(message):
    """Give a plain-text EmailMessage its branded HTML version (unless it has one, or is a technical alert)."""
    if getattr(message, 'alternatives', None) or message.content_subtype == 'html':
        return message
    if 'Traceback (most recent call last)' in (message.body or '') or message.subject.startswith(
            f'[{settings.APP_NAME} error]'):
        return message
    if not hasattr(message, 'attach_alternative'):
        from django.core.mail import EmailMultiAlternatives

        message.__class__ = EmailMultiAlternatives
        message.alternatives = []
    message.attach_alternative(render_html(message.subject, message.body or ''), 'text/html')
    return message



