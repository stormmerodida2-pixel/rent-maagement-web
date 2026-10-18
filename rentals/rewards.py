"""Making paying rent feel rewarding: a tenant's on-time record (and a reference letter to share), rewards the
landlord can offer for paying on time, and a thank-you in the receipt for paying early or on time."""

import datetime
import logging

from django.core import signing
from django.db import IntegrityError, transaction
from django.utils.translation import gettext as _

from reports.highlights import ON_TIME_GRACE, rent_months

from .models import Lease, RentCharge, RewardGiven, RewardRule, Tenant
from .utils import tenant_language

logger = logging.getLogger(__name__)

RECORD_MONTHS = 12        # the record covers the last year
GOOD_TENANT_MONTHS = 6    # at least this many months tracked…
GOOD_TENANT_LATE = 1      # …with at most this many late, for the "Good tenant" badge
REFERENCE_DAYS = 90       # a shared reference link works this long
REFERENCE_SALT = 'rentals.tenant-reference'


def decided_months(lease, today):
    """Rent months whose on-time question is settled: paid on time, or past the grace days."""
    grace = datetime.timedelta(days=max(ON_TIME_GRACE, lease.late_fee_grace_days or 0))
    return [r for r in rent_months(lease) if r[0].due_date <= today and (r[2] or r[0].due_date + grace < today)]


def tenant_record(tenant, today=None):
    """{'on_time', 'months', 'streak', 'good', 'since', 'leases'} over the last RECORD_MONTHS rent months, or None."""
    today = today or datetime.date.today()
    leases = list(tenant.leases.select_related('unit__property').order_by('start_date'))
    rows = sorted((r for lease in leases for r in decided_months(lease, today)), key=lambda r: r[0].due_date)
    if not rows:
        return None
    recent = rows[-RECORD_MONTHS:]
    streak = 0
    for _charge, _covered, on_time in reversed(rows):
        if not on_time:
            break
        streak += 1
    on_time = sum(1 for r in recent if r[2])
    return {
        'on_time': on_time, 'months': len(recent), 'streak': streak,
        'good': len(recent) >= GOOD_TENANT_MONTHS and len(recent) - on_time <= GOOD_TENANT_LATE,
        'since': leases[0].start_date, 'leases': leases,
    }


# --- Reference letter ---------------------------------------------------------------------------------------------

def reference_token(tenant):
    return signing.dumps({'t': tenant.pk, 'k': tenant.portal_token[:8]}, salt=REFERENCE_SALT, compress=True)


def tenant_from_reference(token):
    """The tenant a reference link is for, or None if it's wrong or older than REFERENCE_DAYS. A new portal link
    for the tenant (the landlord resetting it) also cancels their old reference links."""
    try:
        data = signing.loads(token, salt=REFERENCE_SALT, max_age=datetime.timedelta(days=REFERENCE_DAYS))
    except signing.BadSignature:
        return None
    tenant = Tenant.objects.filter(pk=data.get('t')).first()
    return tenant if tenant and tenant.portal_token[:8] == data.get('k') else None


# --- Rewards for paying on time -----------------------------------------------------------------------------------

def reward_progress(lease, rule, today=None):
    """(on-time months in the current run towards the next reward, the months still in this run) for `lease`."""
    today = today or datetime.date.today()
    last = lease.rewards.order_by('-through_period').values_list('through_period', flat=True).first()
    run, earned = 0, []
    for charge, _covered, on_time in decided_months(lease, today):
        if charge.due_date < rule.started_on or (last and charge.period <= last):
            continue
        run = run + 1 if on_time else 0
        if run == rule.months:
            earned.append(charge.period)
            run = 0
    return run, earned


def give_rewards(today=None):
    """Credit each tenant who has completed a run of on-time months under their landlord's reward. Returns how
    many rewards were given."""
    from audit import utils as audit
    from notifications.models import SmsMessage
    from notifications.utils import send_sms

    today = today or datetime.date.today()
    given = 0
    for rule in RewardRule.objects.filter(is_active=True).select_related('owner'):
        for lease in Lease.objects.filter(unit__property__owner=rule.owner, is_active=True).select_related(
                'tenant', 'unit'):
            _run, earned = reward_progress(lease, rule, today)
            for period in earned:
                try:
                    with transaction.atomic():
                        charge = RentCharge.objects.create(
                            lease=lease, kind=RentCharge.Kind.REWARD, amount=-rule.amount, period=today.replace(day=1),
                            due_date=today, description=f'{rule.months} months paid on time')
                        RewardGiven.objects.create(lease=lease, through_period=period, amount=rule.amount,
                                                   charge=charge)
                except IntegrityError:
                    continue  # already given (another run of the job)
                given += 1
                audit.record(owner=rule.owner, action='reward.given', lease=lease,
                             summary=f'{lease.tenant} earned KES {rule.amount:,.0f} off for paying on time '
                                     f'{rule.months} months running.')
                if lease.tenant.phone:
                    with tenant_language(lease.tenant):
                        text = _('Congratulations %(name)s! You paid rent on time %(months)s months in a row, so '
                                 '%(amount)s comes off your rent. Thank you!') % {
                            'name': lease.tenant.full_name.split()[0], 'months': rule.months,
                            'amount': f'KES {rule.amount:,.0f}'}
                    msg = send_sms(lease.tenant.phone, text, lease, SmsMessage.Kind.OTHER)
                    if msg.status == SmsMessage.Status.FAILED:
                        logger.info('Reward SMS to lease %s failed', lease.pk)
    return given


# --- A thank-you in the receipt -----------------------------------------------------------------------------------

def praise(payment):
    """A warm line for the receipt (in the tenant's language; call inside tenant_language) when this payment
    cleared the current rent early or on time, with their run of on-time months. '' otherwise."""
    lease = payment.lease
    balance = lease.balance
    if balance > 0:
        return ''
    paid_on = payment.paid_at.date()
    rows = rent_months(lease)
    current = [r for r in rows if r[0].due_date <= paid_on + datetime.timedelta(days=31)]
    if not current:
        return ''
    charge, _covered, on_time = current[-1]
    # No emoji: it would make every receipt a Unicode SMS, which costs several times as much.
    if balance < 0 or paid_on < charge.due_date:
        line = _('Well done for paying early!')
    elif on_time:
        line = _('Well done for paying on time!')
    else:
        return ''
    streak = 0
    for _c, _cov, ok in reversed([r for r in rows if r[0].due_date <= max(paid_on, charge.due_date)]):
        if not ok:
            break
        streak += 1
    if streak >= 3:
        line += ' ' + _('That is %(count)s months on time in a row.') % {'count': streak}
    return line
