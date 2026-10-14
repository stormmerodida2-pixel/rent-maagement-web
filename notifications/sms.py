"""Outgoing SMS. Sending never raises: failures are recorded on the SmsMessage instead."""

import datetime
import logging

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


def send_sms(phone, body, lease=None, kind=SmsMessage.Kind.OTHER):
    msg = SmsMessage(lease=lease, phone=phone, body=body, kind=kind, status=SmsMessage.Status.FAILED)
    try:
        msg.phone = normalize_phone(phone)
        backend = settings.SMS['BACKEND']
        if backend == 'console':
            logger.info('SMS to %s: %s', msg.phone, body)
            msg.status = SmsMessage.Status.LOGGED
        elif backend == 'africastalking':
            _send_africastalking(msg)
        else:
            raise ValueError(f'Unknown SMS_BACKEND {backend!r}')
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning('SMS to %s failed: %s', phone, exc)
        msg.error = str(exc)[:255]
    msg.save()
    return msg


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
