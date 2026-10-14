"""Outgoing SMS. Sending never raises: failures are recorded on the SmsMessage instead."""

import datetime
import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings

from payments.models import MpesaAccount
from rentals.phone import normalize_phone

from .models import SmsMessage

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


def reminder_text(lease, balance):
    latest = lease.charges.order_by('-period').first()
    when = ''
    if latest:
        when = (
            f', due {latest.due_date:%d %b}' if latest.due_date >= datetime.date.today()
            else ', now overdue'
        )
    account = MpesaAccount.objects.filter(owner_id=lease.unit.property.owner_id).first()
    if account is None or not account.is_complete():
        how = 'Pay online'
    elif account.is_till:
        how = f'Pay via M-Pesa Till {account.pay_number}, or online'
    else:
        how = f'Pay via M-Pesa Paybill {account.pay_number}, account {lease.account_number}, or online'
    return (
        f'Hi {lease.tenant.full_name.split()[0]}, your rent balance for {lease.unit.label} is '
        f'{_kes(balance)}{when}. {how}: {lease.tenant.portal_url()}'
    )


def send_rent_reminder(lease, balance=None):
    balance = lease.balance if balance is None else balance
    return send_sms(lease.tenant.phone, reminder_text(lease, balance), lease, SmsMessage.Kind.REMINDER)


def send_payment_receipt(payment):
    if not settings.SMS['PAYMENT_RECEIPTS']:
        return None
    lease = payment.lease
    balance = lease.balance
    status = f'Balance: {_kes(balance)}.' if balance > 0 else (
        f'You are {_kes(-balance)} in credit.' if balance < 0 else 'You are fully paid up.'
    )
    body = (
        f'Received {_kes(payment.amount)} (M-Pesa {payment.reference}) for {lease.unit.label}. '
        f'Thank you! {status}'
    )
    return send_sms(lease.tenant.phone, body, lease, SmsMessage.Kind.RECEIPT)
