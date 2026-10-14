"""Minimal client for Safaricom's Daraja API: STK Push (Lipa Na M-Pesa Online) and C2B Paybill/Till.

Every call runs against a landlord's own MpesaAccount, so money goes straight to them.
"""

import base64
import datetime
import hashlib
import ipaddress
import logging
from decimal import ROUND_CEILING, Decimal

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext

from rentals import utils as rental_utils
from rentals.models import Lease

from .models import C2BPayment, MpesaAccount, MpesaTransaction, Payment

logger = logging.getLogger(__name__)

BASE_URLS = {
    MpesaAccount.Environment.SANDBOX: 'https://sandbox.safaricom.co.ke',
    MpesaAccount.Environment.PRODUCTION: 'https://api.safaricom.co.ke',
}
TIMEOUT = 30


# Errors a tenant can act on; anything else is the landlord's setup and is hidden from tenants.
TENANT_ERRORS = {'amount', 'busy'}


class MpesaError(Exception):
    def __init__(self, message, code=''):
        super().__init__(message)
        self.code = code  # Daraja errorCode, when there is one


def normalize_phone(value):
    try:
        return rental_utils.normalize_phone(value)
    except ValueError as exc:
        raise MpesaError(str(exc)) from exc


def account_for(lease):
    """The landlord's M-Pesa account for a lease, or MpesaError if it isn't set up."""
    try:
        account = MpesaAccount.objects.get(owner_id=lease.unit.property.owner_id)
    except MpesaAccount.DoesNotExist:
        raise MpesaError("M-Pesa isn't connected yet. Add your Paybill or Till under Settings → M-Pesa.") from None
    if not account.is_complete():
        raise MpesaError('Your M-Pesa settings are incomplete. Finish them under Settings → M-Pesa.')
    return account


def _token_cache_key(account):
    # Include a hash of the credentials so editing them invalidates the cached token.
    digest = hashlib.sha256(f'{account.consumer_key}:{account.consumer_secret}'.encode()).hexdigest()[:16]
    return f'mpesa:token:{account.pk}:{account.environment}:{digest}'


def get_access_token(account, use_cache=True):
    key = _token_cache_key(account)
    token = cache.get(key) if use_cache else None
    if token:
        return token
    if not account.consumer_key or not account.consumer_secret:
        raise MpesaError('Consumer key and secret are required.')
    try:
        resp = requests.get(
            f'{BASE_URLS[account.environment]}/oauth/v1/generate',
            params={'grant_type': 'client_credentials'},
            auth=(account.consumer_key, account.consumer_secret),
            timeout=TIMEOUT,
        )
        if resp.status_code in (400, 401):
            raise MpesaError('Safaricom rejected the consumer key/secret. Check they belong to this environment.')
        resp.raise_for_status()
        data = resp.json()
        token = data['access_token']
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise MpesaError(f'Could not get M-Pesa access token: {exc}') from exc
    # Tokens last 3600s; refresh a little early.
    cache.set(key, token, int(data.get('expires_in', 3599)) - 60)
    return token


def _post(account, path, payload):
    try:
        resp = requests.post(
            f'{BASE_URLS[account.environment]}{path}',
            json=payload,
            headers={'Authorization': f'Bearer {get_access_token(account)}'},
            timeout=TIMEOUT,
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise MpesaError(f'Daraja request to {path} failed: {exc}') from exc
    if resp.status_code != 200 or str(data.get('ResponseCode', '0')) != '0':
        msg = data.get('errorMessage') or data.get('ResponseDescription') or resp.text
        raise MpesaError(f'Daraja rejected the request: {msg}', code=str(data.get('errorCode', '')))
    return data


def hook_url(account, name):
    """Public URL of one of this account's webhooks (Safaricom must reach it over HTTPS)."""
    base = settings.MPESA['CALLBACK_BASE_URL']
    if not base:
        raise MpesaError('MPESA_CALLBACK_BASE_URL must be set to a public HTTPS URL.')
    return base.rstrip('/') + reverse(f'payments:{name}', args=[account.callback_token])


def _parse_timestamp(value):
    try:
        naive = datetime.datetime.strptime(str(value), '%Y%m%d%H%M%S')
    except (TypeError, ValueError):
        return timezone.now()
    return timezone.make_aware(naive)


# How far apart a status-checked STK payment and its late-arriving receipt can be and still match.
RECEIPT_MATCH_WINDOW = datetime.timedelta(minutes=30)


def record_mpesa_payment(lease, receipt, amount, paid_at, notes=''):
    """Create the Payment for an M-Pesa receipt, or return the existing one.

    The same money can reach us up to three ways: the STK callback, the C2B confirmation, and our
    own STK status check (which confirms payment but carries no receipt number). Payments are keyed
    on the receipt; a receipt-less payment from a status check is claimed by the first matching
    receipt instead of creating a duplicate. Returns (payment, created).
    """
    existing = Payment.objects.filter(method=Payment.Method.MPESA, reference=receipt).first()
    if existing:
        return existing, False
    claim = (
        Payment.objects.select_for_update()
        .filter(method=Payment.Method.MPESA, reference='', lease=lease, amount=amount,
                mpesa_transaction__isnull=False,
                paid_at__range=(paid_at - RECEIPT_MATCH_WINDOW, paid_at + RECEIPT_MATCH_WINDOW))
        .first()
    )
    if claim:
        claim.reference = receipt
        claim.save(update_fields=['reference'])
        MpesaTransaction.objects.filter(payment=claim).update(receipt_number=receipt)
        return claim, False

    payment, created = Payment.objects.get_or_create(
        method=Payment.Method.MPESA,
        reference=receipt,
        defaults={'lease': lease, 'amount': amount, 'paid_at': paid_at, 'notes': notes},
    )
    if created:
        _payment_received(payment)
    return payment, created


def _payment_received(payment):
    from audit import utils as audit
    from notifications.utils import send_payment_receipt

    lease = payment.lease
    audit.record(
        owner=lease.unit.property.owner, lease=lease, action='payment.mpesa_received',
        summary=f'M-Pesa payment {payment.reference or "(receipt pending)"} of KES {payment.amount:,.2f} '
                f'received from {lease.tenant}.',
    )
    transaction.on_commit(lambda: send_payment_receipt(payment))


def verify_credentials(account):
    """Check the consumer key/secret by fetching a fresh token. Records the outcome on the account."""
    try:
        get_access_token(account, use_cache=False)
    except MpesaError as exc:
        account.last_error = str(exc)[:255]
        account.verified_at = None
        account.save(update_fields=['last_error', 'verified_at', 'updated_at'])
        raise
    account.last_error = ''
    account.verified_at = timezone.now()
    account.save(update_fields=['last_error', 'verified_at', 'updated_at'])


# --- STK Push -----------------------------------------------------------------

PENDING_PUSH_COOLDOWN = datetime.timedelta(minutes=2)


def stk_push(lease, amount, phone_number=None):
    """Prompt the tenant's phone to pay `amount` against `lease`. Returns the MpesaTransaction."""
    account = account_for(lease)
    # M-Pesa only accepts whole shillings.
    amount = Decimal(amount).to_integral_value(rounding=ROUND_CEILING)
    if amount < 1:
        raise MpesaError(gettext('Amount must be at least KES 1.'), code='amount')
    msisdn = normalize_phone(phone_number or lease.tenant.phone)
    if lease.mpesa_transactions.filter(
        status=MpesaTransaction.Status.PENDING,
        created_at__gte=timezone.now() - PENDING_PUSH_COOLDOWN,
    ).exists():
        raise MpesaError(gettext('A payment prompt was just sent. Wait a couple of minutes and try again.'), code='busy')

    password, timestamp = _stk_password(account)
    data = _post(account, '/mpesa/stkpush/v1/processrequest', {
        'BusinessShortCode': account.shortcode,
        'Password': password,
        'Timestamp': timestamp,
        'TransactionType': account.transaction_type,
        'Amount': int(amount),
        'PartyA': msisdn,
        'PartyB': account.pay_number,
        'PhoneNumber': msisdn,
        'CallBackURL': hook_url(account, 'stk_callback'),
        'AccountReference': lease.account_number[:12],
        'TransactionDesc': f'Rent {lease.unit.label}'[:13],
    })
    return MpesaTransaction.objects.create(
        account=account,
        lease=lease,
        phone=msisdn,
        amount=amount,
        merchant_request_id=data.get('MerchantRequestID', ''),
        checkout_request_id=data['CheckoutRequestID'],
    )


def _stk_password(account):
    timestamp = timezone.localtime().strftime('%Y%m%d%H%M%S')
    password = base64.b64encode(f'{account.shortcode}{account.passkey}{timestamp}'.encode()).decode()
    return password, timestamp


def _apply_stk_success(txn, *, receipt, amount, paid_at, notes):
    """Mark a pending STK transaction paid and credit the tenant. Caller holds the row lock and saves."""
    txn.status = MpesaTransaction.Status.SUCCESS
    txn.receipt_number = receipt
    if receipt:
        txn.payment, _ = record_mpesa_payment(txn.lease, receipt=receipt, amount=amount, paid_at=paid_at, notes=notes)
    else:
        # Status check: Safaricom confirms payment but doesn't say the receipt number. Credit the
        # tenant now; the receipt is filled in if the callback or C2B confirmation turns up later.
        txn.payment = Payment.objects.create(
            lease=txn.lease, amount=amount, method=Payment.Method.MPESA, reference='', paid_at=paid_at, notes=notes,
        )
        _payment_received(txn.payment)


def handle_stk_callback(account, body):
    """Apply an STK callback payload. Idempotent: repeated callbacks are ignored.

    Returns the MpesaTransaction, or None if the CheckoutRequestID is unknown for this account.
    """
    cb = body['Body']['stkCallback']
    checkout_id = cb['CheckoutRequestID']
    result_code = int(cb['ResultCode'])
    items = {item['Name']: item.get('Value') for item in cb.get('CallbackMetadata', {}).get('Item', [])}
    receipt = str(items.get('MpesaReceiptNumber', ''))

    with transaction.atomic():
        txn = (
            MpesaTransaction.objects.select_for_update()
            .filter(checkout_request_id=checkout_id, account=account)
            .first()
        )
        if txn is None:
            logger.warning('STK callback for unknown CheckoutRequestID %s (account %s)', checkout_id, account.pk)
            return None
        if txn.status == MpesaTransaction.Status.SUCCESS and not txn.receipt_number and receipt and result_code == 0:
            # Late callback after a status check already credited the tenant: just add the receipt.
            txn.receipt_number = receipt
            txn.raw_callback = body
            txn.save(update_fields=['receipt_number', 'raw_callback', 'updated_at'])
            if txn.payment_id:
                Payment.objects.filter(pk=txn.payment_id, reference='').update(reference=receipt)
            return txn
        if txn.status != MpesaTransaction.Status.PENDING:
            return txn

        txn.raw_callback = body
        txn.result_code = result_code
        txn.result_desc = cb.get('ResultDesc', '')[:255]

        if result_code != 0:
            txn.status = MpesaTransaction.Status.FAILED
            txn.save()
            return txn

        _apply_stk_success(
            txn, receipt=receipt,
            amount=Decimal(str(items.get('Amount', txn.amount))),
            paid_at=_parse_timestamp(items.get('TransactionDate')),
            notes=f'STK push from {items.get("PhoneNumber", txn.phone)}',
        )
        txn.save()
        return txn


# Daraja answers "still processing" with this error code while the tenant hasn't responded yet.
STK_STILL_PROCESSING = '500.001.1001'
STK_QUERY_AFTER = datetime.timedelta(minutes=2)  # give the callback a chance first
STK_EXPIRE_AFTER = datetime.timedelta(hours=24)


def _expire(txn, reason):
    """Give up on a pending prompt. Returns the transaction."""
    with transaction.atomic():
        txn = MpesaTransaction.objects.select_for_update().get(pk=txn.pk)
        if txn.status == MpesaTransaction.Status.PENDING:
            txn.status = MpesaTransaction.Status.FAILED
            txn.result_desc = reason[:255]
            txn.save()
    return txn


def query_stk_status(txn):
    """Ask Safaricom what happened to a pending STK prompt and apply the answer.

    Prompts still unresolved 24 hours after they were sent are marked failed, whatever stopped
    the check (still processing, credentials changed, account removed), so nothing is retried forever.
    Returns the (possibly updated) transaction. Raises MpesaError if Safaricom can't be asked yet.
    """
    if txn.status != MpesaTransaction.Status.PENDING:
        return txn
    too_old = timezone.now() - txn.created_at > STK_EXPIRE_AFTER
    try:
        account = txn.account
        if account is None or not account.is_complete():
            raise MpesaError('This prompt was sent from an M-Pesa account that is no longer set up.')
        password, timestamp = _stk_password(account)
        data = _post(account, '/mpesa/stkpushquery/v1/query', {
            'BusinessShortCode': account.shortcode,
            'Password': password,
            'Timestamp': timestamp,
            'CheckoutRequestID': txn.checkout_request_id,
        })
    except MpesaError as exc:
        if too_old:
            reason = 'No answer from Safaricom within 24 hours'
            if exc.code != STK_STILL_PROCESSING:
                reason += f'; last check failed: {exc}'
            return _expire(txn, reason)
        if exc.code == STK_STILL_PROCESSING:
            return txn
        raise

    result_code = int(data.get('ResultCode', -1))
    with transaction.atomic():
        txn = MpesaTransaction.objects.select_for_update().get(pk=txn.pk)
        if txn.status != MpesaTransaction.Status.PENDING:
            return txn  # the callback won the race
        txn.result_code = result_code
        txn.result_desc = (data.get('ResultDesc') or '')[:255]
        if result_code == 0:
            _apply_stk_success(
                txn, receipt='', amount=txn.amount, paid_at=txn.created_at,
                notes='STK push confirmed by status check',
            )
        else:
            txn.status = MpesaTransaction.Status.FAILED
        txn.save()
    return txn


def check_pending_stk(now=None):
    """Status-check every STK prompt that has been pending for a while. Returns a summary dict."""
    now = now or timezone.now()
    summary = {'checked': 0, 'paid': 0, 'failed': 0, 'still_pending': 0, 'errors': 0}
    pending = MpesaTransaction.objects.filter(
        status=MpesaTransaction.Status.PENDING, created_at__lte=now - STK_QUERY_AFTER,
    ).select_related('account', 'lease__tenant')
    for txn in pending:
        summary['checked'] += 1
        try:
            txn = query_stk_status(txn)
        except MpesaError as exc:
            logger.warning('STK status check failed for %s: %s', txn.checkout_request_id, exc)
            summary['errors'] += 1
            continue
        key = {'success': 'paid', 'failed': 'failed'}.get(txn.status, 'still_pending')
        summary[key] += 1
    return summary


# --- C2B Paybill / Till ------------------------------------------------------------

def register_c2b_urls(account):
    """Tell Safaricom where to send C2B validation/confirmation requests for this account."""
    data = _post(account, '/mpesa/c2b/v1/registerurl', {
        'ShortCode': account.pay_number,
        # If our validation URL is unreachable, let the payment go through anyway.
        'ResponseType': 'Completed',
        'ConfirmationURL': hook_url(account, 'c2b_confirmation'),
        'ValidationURL': hook_url(account, 'c2b_validation'),
    })
    account.c2b_registered_at = timezone.now()
    account.save(update_fields=['c2b_registered_at', 'updated_at'])
    return data


def callback_is_trusted(account, ip):
    """Did a webhook for this account come from Safaricom? Only enforced for production accounts.

    Safaricom doesn't sign callbacks, so without this anyone who learned an account's webhook URL
    could report payments that never happened.
    """
    if account.environment != MpesaAccount.Environment.PRODUCTION:
        return True
    allowed = settings.MPESA['CALLBACK_IPS']
    if 'any' in allowed:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(net, strict=False) for net in allowed)


def match_lease(account, bill_ref, msisdn):
    """Find which of this landlord's leases a payment belongs to: by account number, else by phone."""
    lease = Lease.find_by_account_number(bill_ref, owner=account.owner)
    if lease:
        return lease
    try:
        number = rental_utils.normalize_phone(msisdn)
    except ValueError:
        return None  # Safaricom now often sends a hashed MSISDN
    leases = list(
        Lease.objects.filter(is_active=True, tenant__phone=number, unit__property__owner=account.owner)[:2]
    )
    return leases[0] if len(leases) == 1 else None


def handle_c2b_confirmation(account, body, trusted=True, source_ip=None):
    """Store a C2B confirmation and, if it can be matched to a lease, record the payment. Idempotent.

    Untrusted confirmations (not from Safaricom's addresses) are stored but held for the landlord
    to review against their M-Pesa statement; nothing is credited. If the genuine confirmation for
    the same receipt arrives later, it releases the held record and is applied normally.
    """
    shortcode = str(body.get('BusinessShortCode', ''))
    if shortcode and shortcode != account.pay_number:
        logger.warning('C2B for shortcode %s arrived on account %s (%s)', shortcode, account.pk, account.pay_number)
    with transaction.atomic():
        c2b, created = C2BPayment.objects.select_for_update().get_or_create(
            trans_id=body['TransID'],
            defaults={
                'account': account,
                'trans_type': body.get('TransactionType', ''),
                'trans_time': _parse_timestamp(body.get('TransTime')),
                'amount': Decimal(str(body['TransAmount'])),
                'shortcode': shortcode,
                'bill_ref': (body.get('BillRefNumber') or '')[:100],
                'msisdn': str(body.get('MSISDN', ''))[:100],
                'payer_name': ' '.join(
                    filter(None, [body.get('FirstName'), body.get('MiddleName'), body.get('LastName')])
                )[:200],
                'raw': body,
                'source_ip': source_ip,
                'held_for_review': not trusted,
            },
        )
        if not created:
            released = trusted and c2b.held_for_review and c2b.payment_id is None and c2b.account_id == account.pk
            if not released:
                return c2b
            # The genuine confirmation: use Safaricom's details, not the earlier untrusted copy.
            c2b.amount = Decimal(str(body['TransAmount']))
            c2b.bill_ref = (body.get('BillRefNumber') or '')[:100]
            c2b.msisdn = str(body.get('MSISDN', ''))[:100]
            c2b.raw, c2b.source_ip, c2b.held_for_review = body, source_ip, False
        if c2b.held_for_review:
            logger.warning('Held C2B %s on account %s: came from %s, not a Safaricom address',
                           c2b.trans_id, account.pk, source_ip)
            from audit import utils as audit

            audit.record(
                owner=account.owner, action='payment.held',
                summary=f'Held M-Pesa payment {c2b.trans_id} (KES {c2b.amount:,.2f}) for review: it came '
                        f"from {source_ip or 'an unknown address'}, not Safaricom. Check your M-Pesa statement.",
            )
            return c2b
        c2b.lease = match_lease(account, c2b.bill_ref, c2b.msisdn)
        if c2b.lease is None:
            logger.warning('Unmatched payment %s on account %s (ref %r)', c2b.trans_id, account.pk, c2b.bill_ref)
        else:
            apply_c2b_payment(c2b)
        c2b.save()
        return c2b


def apply_c2b_payment(c2b):
    """Record the Payment for a C2B payment that has a lease. Caller saves `c2b`."""
    c2b.payment, _ = record_mpesa_payment(
        c2b.lease,
        receipt=c2b.trans_id,
        amount=c2b.amount,
        paid_at=c2b.trans_time,
        notes=f'Paybill, account {c2b.bill_ref!r}'[:255] if c2b.bill_ref else 'Till payment',
    )
