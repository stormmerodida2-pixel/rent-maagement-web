"""The monthly report card: last month's rent results by email (and a short SMS) on the 1st."""

import datetime
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError
from django.template.loader import render_to_string
from django.urls import reverse

from accounts.verification import is_verified
from reports.summary import month_summary
from reports.views import _add_months

from .models import LandlordPreferences, ReportCardSent, SmsMessage
from .sms import send_sms

logger = logging.getLogger(__name__)


def previous_month(today=None):
    today = today or datetime.date.today()
    return _add_months(today.replace(day=1), -1)


def report_url(month):
    return f"{settings.SITE_URL}{reverse('reports:overview')}?month={month:%Y-%m}"


def sms_text(s):
    rate = f" ({s['rate']}%)" if s['rate'] is not None else ''
    return (f"{settings.APP_NAME} {s['month']:%b %Y}: collected KES {s['collected']:,.0f} of {s['billed']:,.0f}{rate}. "
            f"Arrears KES {s['arrears']:,.0f} ({s['owing_count']} tenants). {s['vacant']} vacant. "
            f"Report: {report_url(s['month'])}")


def send_report_card(user, month, record=True):
    """Email and/or SMS one landlord their report card. Returns (email_sent, sms_sent), or None if skipped."""
    summary = month_summary(user, month)
    if not summary['had_activity']:
        return None
    if record:
        try:
            log = ReportCardSent.objects.create(owner=user, month=month)
        except IntegrityError:
            return None  # already sent this month
    prefs = LandlordPreferences.for_user(user)
    context = {'s': summary, 'user': user, 'APP_NAME': settings.APP_NAME, 'url': report_url(month),
               'settings_url': f"{settings.SITE_URL}{reverse('notifications:preferences')}"}

    email_sent = sms_sent = False
    if prefs.report_email and user.email and is_verified(user):
        try:
            message = EmailMultiAlternatives(
                subject=f"{settings.APP_NAME}: your {month:%B %Y} rent report",
                body=render_to_string('notifications/email/report_card.txt', context),
                to=[user.email],
            )
            message.attach_alternative(render_to_string('notifications/email/report_card.html', context), 'text/html')
            message.send()
            email_sent = True
        except Exception:
            logger.exception('Could not email report card to user %s', user.pk)
    if prefs.report_sms and prefs.phone:
        msg = send_sms(prefs.phone, sms_text(summary), kind=SmsMessage.Kind.REPORT)
        sms_sent = msg.status != SmsMessage.Status.FAILED
    if record:
        log.email_sent, log.sms_sent = email_sent, sms_sent
        log.save(update_fields=['email_sent', 'sms_sent'])
    return email_sent, sms_sent


def send_all(month=None):
    """Send last month's report card to every landlord who hasn't had it. Returns how many were sent."""
    month = month or previous_month()
    sent = 0
    already = set(ReportCardSent.objects.filter(month=month).values_list('owner_id', flat=True))
    for user in get_user_model().objects.filter(properties__isnull=False, is_active=True).distinct():
        if user.pk not in already and send_report_card(user, month):
            sent += 1
    return sent
