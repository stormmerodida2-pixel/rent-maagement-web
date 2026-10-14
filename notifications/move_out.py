"""A few days before a tenant moves out, remind the landlord to inspect the house and settle the deposit."""

import datetime
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

from accounts.verification import is_verified
from rentals.models import Lease

from .models import LandlordPreferences, SmsMessage
from .sms import send_sms

logger = logging.getLogger(__name__)

DAYS_BEFORE = 3


def send_move_out_reminders(today=None):
    """Remind each landlord once per notice. Returns how many reminders went out."""
    today = today or datetime.date.today()
    due = (Lease.objects.filter(is_active=True, end_date__gte=today,
                                end_date__lte=today + datetime.timedelta(days=DAYS_BEFORE),
                                move_out_reminded_on__isnull=True)
           .select_related('tenant', 'unit__property__owner'))
    sent = 0
    for lease in due:
        owner = lease.unit.property.owner
        url = settings.SITE_URL.rstrip('/') + reverse('rentals:lease_detail', args=[lease.pk])
        when = f'{lease.end_date:%A %d %B}'
        delivered = False
        if owner.email and is_verified(owner):
            try:
                send_mail(
                    subject=f'{lease.tenant} moves out of {lease.unit.label} on {when}',
                    message=(f'Hi {owner.first_name or owner.username},\n\n'
                             f'{lease.tenant} moves out of {lease.unit} on {when}.\n\n'
                             'Before then: inspect the house with them, take photos, and agree any deductions '
                             'from the deposit.\nOn the day: open their lease and choose "Finish move-out" to '
                             f'settle the deposit and free the unit:\n{url}\n\n{settings.APP_NAME}'),
                    from_email=None, recipient_list=[owner.email],
                )
                delivered = True
            except Exception:
                logger.exception('Could not email move-out reminder for lease %s', lease.pk)
        prefs = LandlordPreferences.for_user(owner)
        if prefs.report_sms and prefs.phone:
            msg = send_sms(prefs.phone, f'{settings.APP_NAME}: {lease.tenant} moves out of {lease.unit.label} on '
                                        f'{lease.end_date:%a %d %b}. Inspect and settle the deposit: {url}')
            delivered = delivered or msg.status != SmsMessage.Status.FAILED
        lease.move_out_reminded_on = today  # once per notice, even if nothing could be delivered
        lease.save(update_fields=['move_out_reminded_on'])
        sent += delivered
    return sent
