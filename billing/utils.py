"""Helpers for billing landlords: status, monthly bills, payments, M-Pesa prompts to the Rentipay Till,
emails, and the read-only rule for unpaid accounts."""

import calendar
import datetime
import logging
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from .models import BillingAccount, BillingSettings, Invoice

logger = logging.getLogger(__name__)


# Fixed rules (the rest is set in Billing overview).
FIXED = {
    'NOTICE_DAYS': 3,  # the bill is sent this many days before it's due
    'CLAIM_DAYS': 3,   # a landlord who entered an M-Pesa code stays unlocked this long while you check it
}


def conf(name):
    """A billing setting by its upper-case name, e.g. conf('PRICE_PER_UNIT')."""
    if name in FIXED:
        return FIXED[name]
    field = name.lower()
    public = BillingSettings.public()
    if field in public:
        return public[field]
    return getattr(BillingSettings.load(), field)  # secret keys and the callback token: not cached


def enabled():
    return conf('ENABLED')


def add_month(day):
    """Same day next month, or the month's last day if it's shorter (31 Jan -> 28 Feb)."""
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


# --- Who pays what -----------------------------------------------------------------------------------

def occupied_units(owner):
    from rentals.models import Lease

    return Lease.objects.filter(is_active=True, unit__property__owner=owner).values('unit').distinct().count()


def monthly_amount(units):
    return max(units - conf('FREE_UNITS'), 0) * conf('PRICE_PER_UNIT')


def account_for(owner, today=None):
    """The landlord's billing account, starting their free trial the first time it's needed."""
    today = today or timezone.localdate()
    trial_ends = today + datetime.timedelta(days=conf('TRIAL_DAYS'))
    account, _ = BillingAccount.objects.get_or_create(owner=owner, defaults={
        'trial_ends': trial_ends, 'paid_until': trial_ends})
    return account


def open_invoice(account):
    return account.invoices.filter(status=Invoice.Status.OPEN).order_by('due_date').first()


def status(account, today=None):
    """'trial', 'active', 'checking' (Till code waiting for you), 'overdue' (in the grace period) or 'locked'."""
    today = today or timezone.localdate()
    if today < account.paid_until:
        return 'trial' if today < account.trial_ends and not account.invoices.filter(
            status=Invoice.Status.PAID).exclude(method=Invoice.Method.FREE).exists() else 'active'
    invoice = open_invoice(account)
    if invoice and invoice.claimed_at and \
            today <= timezone.localdate(invoice.claimed_at) + datetime.timedelta(days=conf('CLAIM_DAYS')):
        return 'checking'
    if today < account.paid_until + datetime.timedelta(days=conf('GRACE_DAYS')):
        return 'overdue'
    return 'locked'


def lock_date(account):
    return account.paid_until + datetime.timedelta(days=conf('GRACE_DAYS'))


def is_locked(owner, today=None):
    """Read-only because of an unpaid bill. Always False while billing is off."""
    if not enabled() or owner.is_superuser:
        return False
    account = BillingAccount.objects.filter(owner=owner).first()
    return account is not None and status(account, today) == 'locked'


def locked_owner_ids(today=None):
    """Landlords whose accounts are read-only right now: their tenants' reminder SMS are paused."""
    if not enabled():
        return set()
    today = today or timezone.localdate()
    candidates = BillingAccount.objects.filter(
        paid_until__lte=today - datetime.timedelta(days=conf('GRACE_DAYS')), owner__is_superuser=False)
    return {account.owner_id for account in candidates if status(account, today) == 'locked'}


# --- Bills and payments -----------------------------------------------------------------------------

def make_invoice(account, today=None):
    """Bill the next month if it starts within NOTICE_DAYS and isn't billed yet. Returns the invoice or None."""
    today = today or timezone.localdate()
    if open_invoice(account) or today < account.paid_until - datetime.timedelta(days=conf('NOTICE_DAYS')):
        return None
    start = account.paid_until
    units = occupied_units(account.owner)
    amount = monthly_amount(units)
    try:
        with transaction.atomic():
            invoice = Invoice.objects.create(
                account=account, period_start=start, period_end=add_month(start), units=units,
                free_units=conf('FREE_UNITS'), price_per_unit=conf('PRICE_PER_UNIT'), amount=amount, due_date=start)
    except IntegrityError:
        return None  # already billed (another run got there first)
    if amount == 0:
        invoice = mark_paid(invoice, Invoice.Method.FREE)
    else:
        email_landlord(invoice, 'bill')
        Invoice.objects.filter(pk=invoice.pk).update(sent_at=timezone.now())
    return invoice


@transaction.atomic
def mark_paid(invoice, method, reference='', actor=None):
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == Invoice.Status.PAID:
        return invoice
    invoice.status, invoice.method, invoice.paid_at = Invoice.Status.PAID, method, timezone.now()
    invoice.reference = (reference or invoice.claimed_reference)[:20] if method != Invoice.Method.FREE else ''
    invoice.save(update_fields=['status', 'method', 'paid_at', 'reference'])
    account = invoice.account
    if invoice.period_end > account.paid_until:
        account.paid_until = invoice.period_end
        account.save(update_fields=['paid_until'])
    if method != Invoice.Method.FREE:
        from audit import utils as audit

        audit.record(owner=account.owner, actor=actor, action='billing.paid',
                     summary=f'Paid Rentipay bill {invoice.number}: KES {invoice.amount:,} '
                             f'({invoice.get_method_display()}{", " + invoice.reference if invoice.reference else ""}).')
        email_landlord(invoice, 'receipt')
    return invoice


MPESA_CODE = re.compile(r'[A-Z0-9]{10}')


class ClaimError(ValueError):
    pass


def claim_till_payment(invoice, code):
    """The landlord paid to the Till and typed the M-Pesa code: unlock while you check it."""
    code = re.sub(r'\s', '', code or '').upper()
    if not MPESA_CODE.fullmatch(code):
        raise ClaimError('Enter the 10-character code from the M-Pesa message, e.g. SJK4H7Q2LP.')
    if Invoice.objects.filter(reference=code).exists() or \
            Invoice.objects.filter(claimed_reference=code).exclude(pk=invoice.pk).exists():
        raise ClaimError('That M-Pesa code has already been used for a bill.')
    invoice.claimed_reference, invoice.claimed_at = code, timezone.now()
    invoice.save(update_fields=['claimed_reference', 'claimed_at'])
    notify_admins(f'{invoice.account.owner} says they paid {invoice.number}',
                  f'{invoice.account.owner} (username {invoice.account.owner.username}) says they paid KES '
                  f'{invoice.amount:,} for {invoice.number} to Till {conf("TILL")}, M-Pesa code {code}.\n\n'
                  f'Check it on your M-Pesa statement, then confirm it in Billing overview: '
                  f'{settings.SITE_URL}{reverse("billing:overview")}')


# --- M-Pesa prompts to the Rentipay Till ---------------------------------------------------------------

def platform_account():
    """The Rentipay Till as an (unsaved) M-Pesa account, or None until its Daraja keys are set."""
    from payments.models import MpesaAccount

    row = BillingSettings.load()
    if not row.prompts_ready():
        return None
    return MpesaAccount(environment=row.mpesa_environment, payment_type=MpesaAccount.PaymentType.TILL,
                        till_number=row.till, shortcode=row.mpesa_store_number, consumer_key=row.mpesa_consumer_key,
                        consumer_secret=row.mpesa_consumer_secret, passkey=row.mpesa_passkey)


def callback_url():
    from payments import mpesa

    base = settings.MPESA['CALLBACK_BASE_URL']
    if not base:
        raise mpesa.MpesaError('MPESA_CALLBACK_BASE_URL must be set to a public HTTPS URL.')
    return base.rstrip('/') + reverse('billing:stk_callback', args=[conf('CALLBACK_TOKEN')])


def send_prompt(invoice, phone):
    """Ask the landlord's phone to pay the bill. Raises payments.mpesa.MpesaError."""
    from payments import mpesa

    account = platform_account()
    if account is None:
        raise mpesa.MpesaError("M-Pesa prompts aren't set up yet. Pay to the Till instead.")
    msisdn = mpesa.normalize_phone(phone)
    password, timestamp = mpesa._stk_password(account)
    data = mpesa._post(account, '/mpesa/stkpush/v1/processrequest', {
        'BusinessShortCode': account.shortcode, 'Password': password, 'Timestamp': timestamp,
        'TransactionType': account.transaction_type, 'Amount': invoice.amount, 'PartyA': msisdn,
        'PartyB': account.pay_number, 'PhoneNumber': msisdn, 'CallBackURL': callback_url(),
        'AccountReference': invoice.number, 'TransactionDesc': 'Rentipay',
    })
    invoice.checkout_request_id = data['CheckoutRequestID']
    invoice.save(update_fields=['checkout_request_id'])
    return invoice


def handle_prompt_callback(body):
    """Safaricom's answer to a bill prompt. Returns the invoice, or None if it isn't one of ours."""
    cb = body['Body']['stkCallback']
    invoice = Invoice.objects.filter(checkout_request_id=cb['CheckoutRequestID']).first()
    if invoice is None or int(cb['ResultCode']) != 0:
        return invoice
    items = {item['Name']: item.get('Value') for item in cb.get('CallbackMetadata', {}).get('Item', [])}
    if int(float(items.get('Amount') or 0)) < invoice.amount:
        logger.warning('Bill prompt %s paid %s, less than %s', invoice.number, items.get('Amount'), invoice.amount)
        return invoice
    return mark_paid(invoice, Invoice.Method.PROMPT, str(items.get('MpesaReceiptNumber', '')))


# --- Daily job --------------------------------------------------------------------------------------

def landlords():
    """People billed: anyone who has added a property (not administrators or staff logins)."""
    return get_user_model().objects.filter(properties__isnull=False, is_superuser=False, is_active=True).distinct()


def run_billing(today=None):
    """Make next month's bills, and send overdue and read-only notices. Returns counts."""
    today = today or timezone.localdate()
    counts = {'bills': 0, 'overdue': 0, 'locked': 0}
    if not enabled():
        return counts
    for owner in landlords():
        account = account_for(owner, today)
        if make_invoice(account, today):
            counts['bills'] += 1
        invoice = open_invoice(account)
        if invoice is None:
            continue
        state = status(account, today)
        if state == 'overdue' and not invoice.overdue_notice_at:
            email_landlord(invoice, 'overdue')
            Invoice.objects.filter(pk=invoice.pk).update(overdue_notice_at=timezone.now())
            counts['overdue'] += 1
        elif state == 'locked' and not invoice.locked_notice_at:
            email_landlord(invoice, 'locked')
            Invoice.objects.filter(pk=invoice.pk).update(locked_notice_at=timezone.now())
            counts['locked'] += 1
    return counts


# --- Emails -------------------------------------------------------------------------------------------

def _how_to_pay(invoice):
    return (f'Pay KES {invoice.amount:,} in Rentipay (Settings → Billing), or from the M-Pesa menu: '
            f'Lipa na M-Pesa → Buy Goods and Services → Till {conf("TILL")}, then enter the M-Pesa code on the '
            f'Billing page: {settings.SITE_URL}{reverse("billing:billing")}')


def email_landlord(invoice, kind):
    owner = invoice.account.owner
    if not owner.email:
        return
    name = owner.get_full_name() or owner.username
    period = f'{invoice.period_start:%d %b} – {invoice.period_end - datetime.timedelta(days=1):%d %b %Y}'
    if kind == 'bill':
        subject = f'Your {settings.APP_NAME} bill: KES {invoice.amount:,}, due {invoice.due_date:%d %b}'
        body = (f'Hi {name},\n\nYour {settings.APP_NAME} bill {invoice.number} for {period} is KES {invoice.amount:,} '
                f'({invoice.billed_units} units at KES {invoice.price_per_unit} after {invoice.free_units} free).'
                f'\n\n{_how_to_pay(invoice)}')
    elif kind == 'overdue':
        subject = f'Your {settings.APP_NAME} bill is overdue'
        body = (f'Hi {name},\n\nBill {invoice.number} (KES {invoice.amount:,}) was due on {invoice.due_date:%d %b}. '
                f'If it isn\'t paid by {lock_date(invoice.account):%d %b}, your account becomes read-only: you can '
                f'still see everything, but not make changes. Your tenants can still pay rent.\n\n{_how_to_pay(invoice)}')
    elif kind == 'locked':
        subject = f'Your {settings.APP_NAME} account is read-only'
        body = (f'Hi {name},\n\nBill {invoice.number} (KES {invoice.amount:,}) hasn\'t been paid, so your account is '
                f'read-only and rent reminders to tenants are paused. Nothing has been deleted, and tenants can '
                f'still pay rent. Pay to switch everything back on.\n\n{_how_to_pay(invoice)}')
    else:  # receipt
        subject = f'Payment received: {settings.APP_NAME} {invoice.number}'
        body = (f'Hi {name},\n\nThank you. We received KES {invoice.amount:,} for {invoice.number} ({period})'
                f'{", M-Pesa " + invoice.reference if invoice.reference else ""}. '
                f'{settings.APP_NAME} is paid until {invoice.account.paid_until:%d %b %Y}.')
    try:
        send_mail(subject, f'{body}\n\n{settings.APP_NAME}', None, [owner.email])
    except Exception:
        logger.exception('Could not email %s about %s', owner, invoice.number)


def notify_admins(subject, body):
    email = conf('NOTIFY_EMAIL') or getattr(settings, 'SUPPORT_EMAIL', '')
    to = [email] if email else list(
        get_user_model().objects.filter(is_superuser=True).exclude(email='').values_list('email', flat=True))
    if to:
        try:
            send_mail(f'{settings.APP_NAME}: {subject}', body, None, to)
        except Exception:
            logger.exception('Could not email admins: %s', subject)


# --- Read-only when unpaid ------------------------------------------------------------------------------

# Paths that still accept changes on a read-only account: paying the bill, and getting in and out.
UNLOCKED_PATHS = ('/billing/', '/logout/', '/login/', '/settings/password/', '/staff/password/')


class ReadOnlyWhenUnpaidMiddleware:
    """An unpaid landlord (and their staff) can look at everything but not change anything."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and enabled() and request.user.is_authenticated \
                and not request.path.startswith(UNLOCKED_PATHS):
            staff = getattr(request, 'staff', None)
            owner = staff.owner if staff is not None else request.user
            if is_locked(owner):
                messages.error(request, 'Your account is read-only until the Rentipay bill is paid. '
                                        'Nothing has been deleted.')
                return redirect('billing:billing' if staff is None else 'staff:home')
        return self.get_response(request)


def banner(user):
    """What the page-top notice should say for this landlord, or None."""
    if not enabled() or not user.is_authenticated or user.is_superuser:
        return None
    account = BillingAccount.objects.filter(owner=user).first()
    if account is None:
        return None
    state, today = status(account), timezone.localdate()
    if state == 'trial' and (account.trial_ends - today).days <= 5:
        return {'state': state, 'text': f'Your free trial ends on {account.trial_ends:%d %b}.'}
    if state in ('overdue', 'locked', 'checking'):
        invoice = open_invoice(account)
        amount = f'KES {invoice.amount:,}' if invoice else 'your bill'
        text = {
            'overdue': f'Your Rentipay bill ({amount}) is overdue. Pay by {lock_date(account):%d %b} to keep making changes.',
            'locked': f'Your account is read-only until the Rentipay bill ({amount}) is paid. Nothing has been deleted.',
            'checking': "Thanks: we're checking your M-Pesa payment. Everything stays switched on meanwhile.",
        }[state]
        return {'state': state, 'text': text}
    return None
