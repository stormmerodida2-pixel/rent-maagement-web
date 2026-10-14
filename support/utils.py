"""Emails and SMS for complaints and support messages. Failures are logged, never raised."""

import logging

from django.conf import settings
from django.core.mail import EmailMessage, send_mail
from django.urls import reverse
from django.utils.translation import gettext as _

from notifications.models import SmsMessage
from notifications.utils import send_sms
from rentals.utils import tenant_language


# --- Notifications -----------------------------------------------------------------------------------

# Emails and SMS for complaints and support messages. Failures are logged, never raised.

logger = logging.getLogger(__name__)


def _site(path):
    return settings.SITE_URL.rstrip('/') + path


def landlord_new_message(complaint, body, is_new):
    owner = complaint.owner
    if not owner.email:
        return
    url = _site(reverse('support:complaint_detail', args=[complaint.pk]))
    what = 'sent you a new message' if is_new else 'replied'
    try:
        send_mail(
            subject=f'{complaint.tenant} {what}: {complaint.subject}',
            message=(f'{complaint.tenant} ({complaint.lease.unit if complaint.lease else "no active lease"}) {what}:\n\n'
                     f'{body}\n\nReply here: {url}\n\n{settings.APP_NAME}'),
            from_email=None, recipient_list=[owner.email],
        )
    except Exception:
        logger.exception('Could not email landlord %s about complaint %s', owner.pk, complaint.pk)


def tenant_reply(complaint):
    url = _site(reverse('support:tenant_complaint', args=[complaint.tenant.portal_token, complaint.pk]))
    with tenant_language(complaint.tenant):
        body = _('Your landlord replied to "%(subject)s". Read it here: %(link)s') % {
            'subject': complaint.subject[:40], 'link': url}
    return send_sms(complaint.tenant.phone, body, complaint.lease, SmsMessage.Kind.OTHER)


def support_message(msg):
    if not settings.SUPPORT_EMAIL:
        logger.warning('SUPPORT_EMAIL is not set; contact message %s saved but not emailed', msg.pk)
        return
    try:
        EmailMessage(
            subject=f'[{settings.APP_NAME}] {msg.get_topic_display()} from {msg.name}',
            body=(f'From: {msg.name} <{msg.email}>' + (f' (user: {msg.user.username})' if msg.user else '') +
                  f'\nPage: {msg.page or "-"}\n\n{msg.message}'),
            to=[settings.SUPPORT_EMAIL], reply_to=[msg.email],
        ).send()
    except Exception:
        logger.exception('Could not email support message %s', msg.pk)
