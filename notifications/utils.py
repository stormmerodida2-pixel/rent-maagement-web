"""Outgoing messages to tenants and landlords: SMS, the monthly report card and move-out reminders."""

import datetime
import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives, send_mail
from django.db import IntegrityError
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from accounts.utils import is_verified
from payments.models import MpesaAccount
from rentals.models import Lease
from rentals.utils import normalize_phone, tenant_language
from reports.utils import _add_months, month_summary

from .models import LandlordPreferences, ReportCardSent, SmsMessage


# --- SMS ---------------------------------------------------------------------------------------------

# Outgoing SMS. Sending never raises: failures are recorded on the SmsMessage instead.

logger = logging.getLogger(__name__)

AT_URLS = {
    'sandbox': 'https://api.sandbox.africastalking.com/version1/messaging',
    'live': 'https://api.africastalking.com/version1/messaging',
}
AT_SUCCESS_CODES = {100, 101, 102}  # Processed, Sent, Queued


def _send_africastalking(msg):
    cfg = settings.SMS
    url = AT_URLS['sandbox' if cfg['AT_USERNAME'] == 'sandbox' else 'live']
    data = {'username': cfg['AT_USERNAME'], 'to': f'+{msg.phone}', 'message': msg.body}
    if cfg['AT_SENDER_ID']:
        data['from'] = cfg['AT_SENDER_ID']
    resp = requests.post(
        url, data=data, headers={'apiKey': cfg['AT_API_KEY'], 'Accept': 'application/json'}, timeout=30
    )
    resp.raise_for_status()
    recipients = resp.json()['SMSMessageData']['Recipients']
    if not recipients:
        raise ValueError(resp.json()['SMSMessageData'].get('Message', 'No recipients accepted'))
    r = recipients[0]
    msg.provider_message_id = r.get('messageId', '')
    msg.cost = r.get('cost', '')
    if r.get('statusCode') not in AT_SUCCESS_CODES:
        raise ValueError(r.get('status', 'Rejected'))
    msg.status = SmsMessage.Status.SENT


def _deliver(msg):
    """Send one unsaved SmsMessage and record the outcome on it. No database access, so it's thread-safe."""
    phone = msg.phone
    try:
        msg.phone = normalize_phone(phone)
        backend = settings.SMS['BACKEND']
        if backend == 'console':
            logger.info('SMS to %s: %s', msg.phone, msg.body)
            msg.status = SmsMessage.Status.LOGGED
        elif backend == 'africastalking':
            _send_africastalking(msg)
        else:
            raise ValueError(f'Unknown SMS_BACKEND {backend!r}')
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning('SMS to %s failed: %s', phone, exc)
        msg.error = str(exc)[:255]
    return msg


def send_sms(phone, body, lease=None, kind=SmsMessage.Kind.OTHER):
    msg = SmsMessage(lease=lease, phone=phone, body=body, kind=kind, status=SmsMessage.Status.FAILED)
    _deliver(msg)
    msg.save()
    return msg


PARALLEL_SENDS = 8


def send_personal_sms(items, kind=SmsMessage.Kind.OTHER):
    """Send a different message to each of [(lease, phone, body)], several at a time. Never raises.

    Returns the saved SmsMessages in the same order. Only the network calls run in threads.
    """
    msgs = [SmsMessage(lease=lease, phone=phone, body=body, kind=kind, status=SmsMessage.Status.FAILED)
            for lease, phone, body in items]
    with ThreadPoolExecutor(max_workers=PARALLEL_SENDS) as pool:
        list(pool.map(_deliver, msgs))
    return SmsMessage.objects.bulk_create(msgs)


BULK_BATCH = 100  # numbers per Africa's Talking request


def send_bulk_sms(recipients, body, kind=SmsMessage.Kind.OTHER):
    """Send the same text to many people in as few requests as possible.

    `recipients` is a list of (lease, phone). Returns one SmsMessage per recipient, each recording
    its own outcome. Like send_sms, this never raises.
    """
    messages = []
    for lease, phone in recipients:
        msg = SmsMessage(lease=lease, phone=phone, body=body, kind=kind, status=SmsMessage.Status.FAILED)
        try:
            msg.phone = normalize_phone(phone)
        except ValueError as exc:
            msg.error = str(exc)[:255]
        messages.append(msg)
    valid = [m for m in messages if not m.error]

    backend = settings.SMS['BACKEND']
    if backend == 'console':
        for msg in valid:
            logger.info('SMS to %s: %s', msg.phone, body)
            msg.status = SmsMessage.Status.LOGGED
    elif backend == 'africastalking':
        for start in range(0, len(valid), BULK_BATCH):
            _send_africastalking_bulk(valid[start:start + BULK_BATCH], body)
    else:
        for msg in valid:
            msg.error = f'Unknown SMS_BACKEND {backend!r}'
    SmsMessage.objects.bulk_create(messages)
    return messages


def _send_africastalking_bulk(batch, body):
    cfg = settings.SMS
    url = AT_URLS['sandbox' if cfg['AT_USERNAME'] == 'sandbox' else 'live']
    data = {'username': cfg['AT_USERNAME'], 'to': ','.join(f'+{m.phone}' for m in batch), 'message': body}
    if cfg['AT_SENDER_ID']:
        data['from'] = cfg['AT_SENDER_ID']
    try:
        resp = requests.post(url, data=data, headers={'apiKey': cfg['AT_API_KEY'], 'Accept': 'application/json'},
                             timeout=60)
        resp.raise_for_status()
        results = {r.get('number', '').lstrip('+'): r for r in resp.json()['SMSMessageData']['Recipients']}
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning('Bulk SMS batch of %s failed: %s', len(batch), exc)
        for msg in batch:
            msg.error = str(exc)[:255]
        return
    for msg in batch:
        r = results.get(msg.phone)
        if r is None:
            msg.error = 'Not accepted by the SMS provider'
            continue
        msg.provider_message_id = r.get('messageId', '')
        msg.cost = r.get('cost', '')
        if r.get('statusCode') in AT_SUCCESS_CODES:
            msg.status = SmsMessage.Status.SENT
        else:
            msg.error = str(r.get('status', 'Rejected'))[:255]


def _kes(amount):
    return f'KES {amount:,.0f}'


def _how_to_pay(lease):
    """"Pay via M-Pesa Paybill …" in the active language (call inside tenant_language)."""
    account = MpesaAccount.objects.filter(owner_id=lease.unit.property.owner_id).first()
    if account is None or not account.has_pay_number:
        return _('Pay online')
    if account.is_till:
        return _('Pay via M-Pesa Till %(till)s, or online') % {'till': account.pay_number}
    return _('Pay via M-Pesa Paybill %(paybill)s, account %(account)s, or online') % {
        'paybill': account.pay_number, 'account': lease.account_number}


def reminder_text(lease, balance):
    """The reminder SMS, in the tenant's language."""
    with tenant_language(lease.tenant):
        latest = lease.charges.order_by('-period').first()
        when = ''
        if latest:
            when = (
                _(', due %(date)s') % {'date': date_format(latest.due_date, 'd M')}
                if latest.due_date >= datetime.date.today() else _(', now overdue')
            )
        how = _how_to_pay(lease)
        return _('Hi %(name)s, your rent balance for %(unit)s is %(balance)s%(when)s. %(how)s: %(link)s') % {
            'name': lease.tenant.full_name.split()[0], 'unit': lease.unit.label, 'balance': _kes(balance),
            'when': when, 'how': how, 'link': lease.tenant.portal_url(),
        }


def send_rent_reminder(lease, balance=None):
    balance = lease.balance if balance is None else balance
    return send_sms(lease.tenant.phone, reminder_text(lease, balance), lease, SmsMessage.Kind.REMINDER)


def receipt_text(payment, balance):
    """The payment receipt SMS, in the tenant's language."""
    lease = payment.lease
    with tenant_language(lease.tenant):
        status = _balance_status(balance)
        return _('Received %(amount)s (M-Pesa %(reference)s) for %(unit)s. Thank you! %(status)s') % {
            'amount': _kes(payment.amount), 'reference': payment.reference, 'unit': lease.unit.label,
            'status': status,
        }


def _balance_status(balance):
    if balance > 0:
        return _('Balance: %(amount)s.') % {'amount': _kes(balance)}
    if balance < 0:
        return _('You are %(amount)s in credit.') % {'amount': _kes(-balance)}
    return _('You are fully paid up.')


def cash_receipt_text(payment, balance, recorded_by):
    """Receipt for cash a caretaker recorded, so the tenant can see it was written down."""
    lease = payment.lease
    with tenant_language(lease.tenant):
        return _('Received %(amount)s cash for %(unit)s, recorded by %(name)s. Thank you! %(status)s') % {
            'amount': _kes(payment.amount), 'unit': lease.unit.label, 'name': recorded_by,
            'status': _balance_status(balance),
        }


def send_cash_receipt(payment, recorded_by):
    if not settings.SMS['PAYMENT_RECEIPTS']:
        return None
    lease = payment.lease
    return send_sms(lease.tenant.phone, cash_receipt_text(payment, lease.balance, recorded_by), lease,
                    SmsMessage.Kind.RECEIPT)


def rent_change_text(change):
    """Tells a tenant their rent is changing, and from which rent day, in their language."""
    lease = change.lease
    with tenant_language(lease.tenant):
        return _('Hi %(name)s, your rent for %(unit)s will change from %(old)s to %(new)s a month, starting with '
                 'the rent due on %(date)s. Details: %(link)s') % {
            'name': lease.tenant.full_name.split()[0], 'unit': lease.unit.label, 'old': _kes(change.old_rent),
            'new': _kes(change.new_rent), 'date': date_format(change.starts_on, 'j M Y'),
            'link': lease.tenant.portal_url(),
        }


def send_payment_receipt(payment):
    if not settings.SMS['PAYMENT_RECEIPTS']:
        return None
    lease = payment.lease
    return send_sms(lease.tenant.phone, receipt_text(payment, lease.balance), lease, SmsMessage.Kind.RECEIPT)


# --- Monthly report card -----------------------------------------------------------------------------

# The monthly report card: last month's rent results by email (and a short SMS) on the 1st.

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


# --- Move-out reminders ------------------------------------------------------------------------------

# A few days before a tenant moves out, remind the landlord to inspect the house and settle the deposit.

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
                    message=(f'Hi {owner.first_name or "there"},\n\n'
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


# --- Payment plans ---------------------------------------------------------------------------------------

PLAN_REMIND_DAYS_BEFORE = 2


def plan_agreed_text(plan):
    """Sent when a plan is set up, so the tenant has the agreement in writing."""
    lease = plan.lease
    first = plan.instalments.first()
    count = plan.instalments.count()
    with tenant_language(lease.tenant):
        every = {'weekly': _('every week'), 'fortnightly': _('every 2 weeks'), 'monthly': _('every month')}[plan.frequency]
        return _('Hi %(name)s, as agreed you will clear %(total)s in %(count)s payments of %(each)s %(every)s, '
                 'starting %(date)s, on top of your normal rent. %(how)s: %(link)s') % {
            'name': lease.tenant.full_name.split()[0], 'total': _kes(plan.total), 'count': count,
            'each': _kes(plan.instalment), 'every': every, 'date': date_format(first.due_date, 'D j M'),
            'how': _how_to_pay(lease), 'link': lease.tenant.portal_url(),
        }


def plan_reminder_text(plan, instalment, amount_due):
    lease = plan.lease
    with tenant_language(lease.tenant):
        return _('Hi %(name)s, your payment plan instalment of %(amount)s is due on %(date)s. %(how)s: %(link)s') % {
            'name': lease.tenant.full_name.split()[0], 'amount': _kes(amount_due),
            'date': date_format(instalment.due_date, 'D j M'), 'how': _how_to_pay(lease),
            'link': lease.tenant.portal_url(),
        }


def send_plan_reminders(today=None):
    """SMS tenants two days before each plan instalment (once each). Completes plans that are cleared."""
    from rentals.models import PaymentPlan
    from rentals.utils import refresh_plan

    today = today or datetime.date.today()
    sent = 0
    from billing.utils import locked_owner_ids

    plans = PaymentPlan.objects.filter(status=PaymentPlan.Status.ACTIVE) \
        .exclude(lease__unit__property__owner_id__in=locked_owner_ids()) \
        .select_related('lease__tenant', 'lease__unit__property')
    for plan in plans:
        progress = refresh_plan(plan, today)
        if progress['done']:
            continue
        # The next instalment still to come; a missed one is rolled into its amount below.
        nxt = next((r['instalment'] for r in progress['rows']
                    if r['state'] != 'paid' and r['instalment'].due_date >= today), None)
        if nxt is None or nxt.reminded_on is not None:
            continue
        if nxt.due_date - datetime.timedelta(days=PLAN_REMIND_DAYS_BEFORE) > today:
            continue
        # What they need to have paid by that date to be on track (includes anything missed before it).
        running = sum((r['instalment'].amount for r in progress['rows']
                       if r['instalment'].due_date <= nxt.due_date), Decimal('0'))
        amount_due = running - progress['paid']
        send_sms(plan.lease.tenant.phone, plan_reminder_text(plan, nxt, amount_due), plan.lease,
                 SmsMessage.Kind.REMINDER)
        nxt.reminded_on = today
        nxt.save(update_fields=['reminded_on'])
        sent += 1
    return sent


# --- WhatsApp payment summaries for landlords ----------------------------------------------------------
# At 8pm, landlords who received payments that day get one WhatsApp message: how much, how, and who still
# owes. Nothing on days without payments. Sent from Rentipay's WhatsApp Business number with a template
# approved by Meta (see Settings → WhatsApp); by SMS until that's set up.

WHATSAPP_API = 'https://graph.facebook.com/v21.0'


class WhatsAppError(Exception):
    pass


def send_whatsapp_template(to, params, conf=None):
    """Send the approved template to `to` (2547…) with its {{1}}…{{n}} filled in. Raises WhatsAppError."""
    from .models import WhatsAppSettings

    conf = conf or WhatsAppSettings.load()
    if not conf.ready():
        raise WhatsAppError("WhatsApp isn't set up yet (Settings → WhatsApp).")
    payload = {
        'messaging_product': 'whatsapp', 'to': to, 'type': 'template',
        'template': {'name': conf.template_name, 'language': {'code': conf.template_language},
                     'components': [{'type': 'body',
                                     'parameters': [{'type': 'text', 'text': str(p)} for p in params]}]},
    }
    try:
        resp = requests.post(f'{WHATSAPP_API}/{conf.phone_number_id}/messages', json=payload, timeout=20,
                             headers={'Authorization': f'Bearer {conf.access_token}'})
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise WhatsAppError(f"Couldn't reach WhatsApp: {exc}") from exc
    if resp.status_code != 200:
        error = data.get('error', {}) if isinstance(data, dict) else {}
        raise WhatsAppError(error.get('error_user_msg') or error.get('message') or f'HTTP {resp.status_code}')
    return data


def payment_summary(owner, day):
    """The four parts of a landlord's summary for `day`, or None if nothing was paid that day."""
    from payments.models import Payment

    payments = list(Payment.objects.filter(lease__unit__property__owner=owner, paid_at__date=day)
                    .select_related('recorded_by'))
    if not payments:
        return None
    mpesa = sum(1 for p in payments if p.method == Payment.Method.MPESA)
    bank = sum(1 for p in payments if p.method == Payment.Method.BANK)
    cash_by = {}
    for p in payments:
        if p.method == Payment.Method.CASH:
            who = ('you' if p.recorded_by_id == owner.pk else (p.recorded_by.first_name or 'staff')) \
                if p.recorded_by else ''
            cash_by[who] = cash_by.get(who, 0) + 1
    parts = [f'{mpesa} M-Pesa'] if mpesa else []
    # Caretakers by name first (the cash an absent landlord most wants to see), then their own.
    for who in sorted(cash_by, key=lambda w: (w == 'you', w)):
        parts.append(f'{cash_by[who]} cash' + (f' by {who}' if who else ''))
    parts += [f'{bank} bank'] if bank else []
    owing = [b for b in (lease.balance for lease in Lease.objects.filter(is_active=True, unit__property__owner=owner))
             if b > 0]
    still = (f'{len(owing)} tenant{"s" if len(owing) != 1 else ""} still owe{"s" if len(owing) == 1 else ""} '
             f'{_kes(sum(owing))}.') if owing else 'Everyone is paid up.'
    return [f'{day:%a %d %b}', _kes(sum(p.amount for p in payments)), ', '.join(parts), still]


def summary_text(parts):
    """The message, in the same words as the WhatsApp template (also used for the SMS fallback)."""
    day, total, how, still = parts
    return (f'{settings.APP_NAME} update for {day}: {total} received ({how}). {still} '
            'You can turn these messages off in Settings → Notifications.')


def summary_phone(owner, prefs):
    from accounts.models import Profile

    return prefs.phone or Profile.objects.filter(user=owner).values_list('phone', flat=True).first() or ''


def send_payment_summaries(day=None):
    """Send each landlord who was paid on `day` (default today) their summary, once. Returns counts."""
    from django.utils import timezone

    from billing.utils import locked_owner_ids
    from .models import WhatsAppSettings

    day = day or timezone.localdate()
    conf = WhatsAppSettings.load()
    counts = {'whatsapp': 0, 'sms': 0, 'failed': 0}
    owners = get_user_model().objects.filter(
        properties__units__leases__payments__paid_at__date=day, is_active=True,
    ).exclude(pk__in=locked_owner_ids()).distinct()
    for owner in owners:
        prefs = LandlordPreferences.for_user(owner)
        phone = summary_phone(owner, prefs)
        if not prefs.payment_summary or prefs.summary_sent_on == day or not phone:
            continue
        parts = payment_summary(owner, day)
        if parts is None:
            continue
        try:
            if conf.ready():
                send_whatsapp_template(phone, parts, conf)
                counts['whatsapp'] += 1
            else:
                send_sms(phone, summary_text(parts))
                counts['sms'] += 1
        except WhatsAppError as exc:
            logger.warning('WhatsApp summary to %s failed (%s); sending by SMS', owner.pk, exc)
            send_sms(phone, summary_text(parts))
            counts['failed'] += 1
        LandlordPreferences.objects.filter(pk=prefs.pk).update(summary_sent_on=day)
    return counts
