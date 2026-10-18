"""Tenants say "I've paid" from their rent page; the landlord confirms (the payment is recorded and the receipt goes
out) or rejects it with a reason the tenant is texted."""

import datetime
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from audit import utils as audit
from rentals.utils import tenant_language

from .models import Payment, PaymentClaim

logger = logging.getLogger(__name__)

MAX_PENDING = 3  # per tenancy, so the landlord's list can't be flooded


class ClaimError(Exception):
    pass


def duplicate_of(claim):
    """A payment already recorded with the same M-Pesa code or bank reference, if there is one."""
    if not claim.reference:
        return None
    return Payment.objects.filter(method=claim.method, reference__iexact=claim.reference).first()


def notify_landlord(claim):
    from accounts.utils import is_verified
    from django.core.mail import send_mail

    owner = claim.lease.unit.property.owner
    if not (owner.email and is_verified(owner)):
        return
    url = settings.SITE_URL.rstrip('/') + reverse('payments:claims')
    try:
        send_mail(f'{claim.lease.tenant} says they paid KES {claim.amount:,.0f}',
                  f'Hi {owner.first_name or "there"},\n\n{claim.lease.tenant} ({claim.lease.unit}) says they paid '
                  f'KES {claim.amount:,.0f} by {claim.get_method_display().lower()} on {claim.paid_on:%d %b %Y}'
                  + (f', reference {claim.reference}' if claim.reference else '') + '.'
                  + (' They attached a photo.' if claim.photo else '')
                  + f'\n\nCheck it against your statement, then confirm it (their receipt goes out) or reject it:\n\n'
                  f'{url}\n\n{settings.APP_NAME}', None, [owner.email])
    except Exception:
        logger.exception('Could not email landlord %s about payment claim %s', owner.pk, claim.pk)


def receipt_text(payment, balance):
    """The tenant's receipt for a payment their landlord confirmed, in their language."""
    from notifications.utils import _balance_status, _kes, _praise

    lease = payment.lease
    with tenant_language(lease.tenant):
        how = payment.get_method_display() + (f' {payment.reference}' if payment.reference else '')
        status = ' '.join(filter(None, [_balance_status(balance), _praise(payment)]))
        return _('Your landlord confirmed %(amount)s (%(how)s) for %(unit)s. Thank you! %(status)s') % {
            'amount': _kes(payment.amount), 'how': how, 'unit': lease.unit.label, 'status': status}


def confirm(claim, user):
    """Record the payment and send the receipt. Raises ClaimError if it's already recorded or no longer pending."""
    from notifications.models import SmsMessage
    from notifications.utils import send_sms

    if claim.status != PaymentClaim.Status.PENDING:
        raise ClaimError('This has already been dealt with.')
    if duplicate_of(claim):
        raise ClaimError(f'A payment with reference {claim.reference} is already recorded. Reject this one as a '
                         'duplicate.')
    paid_at = timezone.make_aware(datetime.datetime.combine(claim.paid_on, datetime.time(12)))
    try:
        with transaction.atomic():
            payment = Payment.objects.create(
                lease=claim.lease, amount=claim.amount, method=claim.method, reference=claim.reference,
                paid_at=min(paid_at, timezone.now()), recorded_by=user,
                notes=('Confirmed from the tenant\'s "I\'ve paid"' + (f': {claim.note}' if claim.note else ''))[:255])
            claim.status, claim.payment = PaymentClaim.Status.CONFIRMED, payment
            claim.decided_by, claim.decided_at = user, timezone.now()
            claim.save(update_fields=['status', 'payment', 'decided_by', 'decided_at'])
    except IntegrityError as exc:
        raise ClaimError(f'A payment with reference {claim.reference} is already recorded.') from exc
    audit.record(owner=claim.lease.unit.property.owner, actor=user, lease=claim.lease, action='payment.claim_confirmed',
                 summary=f'Confirmed {claim.lease.tenant}\'s payment of KES {claim.amount:,.0f} '
                         f'({claim.get_method_display()}{" " + claim.reference if claim.reference else ""}).')
    if settings.SMS['PAYMENT_RECEIPTS'] and claim.lease.tenant.phone:
        send_sms(claim.lease.tenant.phone, receipt_text(payment, claim.lease.balance), claim.lease,
                 SmsMessage.Kind.RECEIPT)
    return payment


def reject(claim, user, reason):
    from notifications.models import SmsMessage
    from notifications.utils import _kes, send_sms

    if claim.status != PaymentClaim.Status.PENDING:
        raise ClaimError('This has already been dealt with.')
    claim.status, claim.reject_reason = PaymentClaim.Status.REJECTED, reason[:200]
    claim.decided_by, claim.decided_at = user, timezone.now()
    claim.save(update_fields=['status', 'reject_reason', 'decided_by', 'decided_at'])
    audit.record(owner=claim.lease.unit.property.owner, actor=user, lease=claim.lease, action='payment.claim_rejected',
                 summary=f'Didn\'t confirm {claim.lease.tenant}\'s "I\'ve paid" of KES {claim.amount:,.0f}: {reason}')
    tenant = claim.lease.tenant
    if tenant.phone:
        with tenant_language(tenant):
            text = _('Hi %(name)s, your landlord could not confirm the %(amount)s you said you paid on %(date)s. '
                     'Reason: %(reason)s') % {'name': tenant.full_name.split()[0], 'amount': _kes(claim.amount),
                                              'date': claim.paid_on.strftime('%d %b'), 'reason': reason}
        send_sms(tenant.phone, text, claim.lease, SmsMessage.Kind.OTHER)
