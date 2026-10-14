"""Helpers for the rentals app: phone numbers, tenant language, billing, rent changes and view mixins."""

import datetime
import re
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import redirect
from django.utils import timezone, translation

from audit import utils as audit
from notifications.models import SmsMessage

from .models import MeterReading, RentChange, RentCharge, Tenant


# --- Phone numbers -----------------------------------------------------------------------------------

def normalize_phone(phone):
    """Convert Kenyan numbers like 0712 345 678 / +254712345678 / 712345678 to 254712345678.

    Raises ValueError for anything that is not a Kenyan mobile number.
    """
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('254'):
        digits = digits[3:]
    elif digits.startswith('0'):
        digits = digits[1:]
    if not re.fullmatch(r'[17]\d{8}', digits):
        raise ValueError(f'{phone!r} is not a valid Kenyan mobile number.')
    return '254' + digits


# --- Tenant language ---------------------------------------------------------------------------------

# Speaking to tenants in their own language (English or Kiswahili).

def tenant_language(tenant):
    """Context manager: translate everything inside into the tenant's language."""
    return translation.override(tenant.language if tenant else 'en')


def tenant_page(view):
    """Decorator for views reached by a tenant's private link (`token` argument)."""

    @wraps(view)
    def wrapper(request, token, *args, **kwargs):
        language = Tenant.objects.filter(portal_token=token).values_list('language', flat=True).first() or 'en'
        with translation.override(language):
            response = view(request, token, *args, **kwargs)
            if hasattr(response, 'render') and not response.is_rendered:
                response.render()
        response.headers.setdefault('Content-Language', language)
        return response

    return wrapper


# --- Billing: water readings, move-out, late fees ----------------------------------------------------

# Billing operations beyond monthly rent: water from meter readings and move-out settlement.

class BillingError(Exception):
    pass


def format_number(value):
    """Format a Decimal without trailing zeros: 1212.50 -> '1212.5', 1200.00 -> '1200'."""
    return f'{Decimal(value).normalize():f}'


def last_reading(unit):
    return unit.meter_readings.order_by('-reading_date', '-created_at').first()


@transaction.atomic
def record_water_reading(unit, reading_date, value):
    """Save a meter reading and bill the consumption since the previous one to the unit's tenant.

    The first reading for a unit is just a baseline. Returns the MeterReading.
    """
    value = Decimal(value)
    previous = last_reading(unit)
    if previous is not None:
        if reading_date < previous.reading_date:
            raise BillingError(f'{unit.label}: date is before the last reading ({previous.reading_date:%d %b %Y}).')
        if value < previous.value:
            raise BillingError(f'{unit.label}: {value} is lower than the last reading ({previous.value}).')

    reading = MeterReading(unit=unit, reading_date=reading_date, value=value)
    rate = unit.property.water_rate
    lease = unit.leases.filter(is_active=True).first()
    if previous is not None and rate and lease is not None:
        used = value - previous.value
        if used > 0:
            amount = (used * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            reading.charge = RentCharge.objects.create(
                lease=lease, kind=RentCharge.Kind.WATER, period=reading_date.replace(day=1),
                amount=amount, due_date=reading_date,
                description=(f'Water {format_number(used)} units ({format_number(previous.value)} → '
                             f'{format_number(value)}) @ KES {format_number(rate)}'),
            )
    reading.save()
    return reading


@transaction.atomic
def settle_move_out(lease, end_date, deductions=Decimal('0'), reason='', apply_deposit=True, refund_paid=False):
    """End a lease and settle the deposit.

    Adds any deductions (damage, cleaning…), applies the deposit held against what's owed, and,
    if the landlord has paid back what's left, records that refund so the account closes at zero.
    Returns the final balance (positive = tenant still owes, negative = refund still due).
    """
    period = end_date.replace(day=1)
    if deductions:
        RentCharge.objects.create(
            lease=lease, kind=RentCharge.Kind.DEDUCTION, period=period, amount=deductions, due_date=end_date,
            description=f'Move-out deduction: {reason}' if reason else 'Move-out deduction',
        )
    held = lease.deposit_held
    if apply_deposit and held > 0:
        RentCharge.objects.create(
            lease=lease, kind=RentCharge.Kind.DEPOSIT_APPLIED, period=period, amount=-held, due_date=end_date,
            description='Deposit applied at move-out',
        )
    balance = lease.balance
    if refund_paid and balance < 0:
        RentCharge.objects.create(
            lease=lease, kind=RentCharge.Kind.REFUND, period=period, amount=-balance, due_date=end_date,
            description='Deposit refund paid to tenant',
        )
        balance = Decimal('0')
    lease.end_date = end_date
    lease.is_active = False
    lease.save(update_fields=['end_date', 'is_active'])
    return balance


def apply_late_fees(leases, today=None):
    """Add late fees across leases. Returns the charges created."""
    today = today or datetime.date.today()
    return [c for c in (lease.apply_late_fee(today) for lease in leases) if c]


@transaction.atomic
def void_reading(reading, user, reason):
    """Void a unit's latest meter reading and the water charge it created.

    Only the latest reading can be voided: each charge is worked out from the reading before it,
    so voiding an older one would leave the next charge wrong. Void back from the newest instead.
    """
    latest = last_reading(reading.unit)
    if latest is None or latest.pk != reading.pk:
        raise BillingError(
            f'{reading.unit.label}: only the latest reading can be voided. Void the newer readings first.'
        )
    reading.void(user, reason)
    if reading.charge_id:
        charge = RentCharge.all_objects.get(pk=reading.charge_id)
        if not charge.is_voided:
            charge.void(user, f'Meter reading voided: {reason}')
    return reading


# --- Planned rent changes ----------------------------------------------------------------------------

# Planning rent changes for month-to-month leases (one lease, or a whole property at once).

def new_rent_for(old, mode, value):
    """The new rent for one lease: 'percent' (+10 → 10% more), 'amount' (+1000) or 'set' (20000)."""
    if mode == 'percent':
        new = old * (1 + value / Decimal('100'))
    elif mode == 'amount':
        new = old + value
    else:
        new = value
    # Whole shillings, rounded to the nearest 50 for percentage rises (KES 18,900 not 18,937.50).
    step = Decimal('50') if mode == 'percent' else Decimal('1')
    return max((new / step).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * step, Decimal('1'))


def plan(leases_and_rents, effective_from, *, notify, actor):
    """Create (or replace) the pending change on each lease. Returns the changes, plus SMS if notified."""
    from notifications.utils import rent_change_text, send_personal_sms  # notifications imports this module

    changes = []
    with transaction.atomic():
        for lease, new_rent in leases_and_rents:
            RentChange.objects.filter(lease=lease, applied_at__isnull=True).delete()
            change = RentChange.objects.create(lease=lease, old_rent=lease.monthly_rent, new_rent=new_rent,
                                               effective_from=effective_from)
            audit.record(owner=actor, actor=actor, action='lease.rent_change_planned', lease=lease,
                         summary=f'Rent for {lease.tenant} changes from KES {change.old_rent:,.0f} to '
                                 f'KES {change.new_rent:,.0f} from {change.starts_on:%d %b %Y}.')
            changes.append(change)
    messages = []
    if notify and changes:
        messages = send_personal_sms([(c.lease, c.lease.tenant.phone, rent_change_text(c)) for c in changes],
                                     kind=SmsMessage.Kind.OTHER)
        sent = {m.lease_id for m in messages if m.status != SmsMessage.Status.FAILED}
        RentChange.objects.filter(pk__in=[c.pk for c in changes if c.lease_id in sent]).update(notified_at=timezone.now())
    return changes, messages


def cancel(change, *, actor):
    lease = change.lease
    change.delete()
    audit.record(owner=actor, actor=actor, action='lease.rent_change_cancelled', lease=lease,
                 summary=f'Cancelled the planned rent change for {lease.tenant}.')


# --- View mixins -------------------------------------------------------------------------------------

class OwnedMixin(LoginRequiredMixin):
    """Limit a view's objects to the signed-in landlord's own records.

    Set `owner_path` to the lookup from the model to its landlord, e.g. 'unit__property__owner'.
    Objects belonging to someone else 404, exactly like ones that don't exist.
    """

    owner_path = 'owner'
    form_takes_owner = True

    def get_queryset(self):
        return super().get_queryset().filter(**{self.owner_path: self.request.user})

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        if self.form_takes_owner:
            kwargs['owner'] = self.request.user
        return kwargs


class SafeDeleteMixin:
    """Refuse to delete records that payments or leases still depend on, with a friendly message."""

    protected_message = "This can't be deleted because other records depend on it."

    def form_valid(self, form):
        label, kind = str(self.object), self.object._meta.verbose_name
        try:
            response = super().form_valid(form)
        except ProtectedError:
            messages.error(self.request, self.protected_message)
            return redirect(self.request.path)
        audit.record(owner=self.request.user, actor=self.request.user,
                     action=f'{kind}.deleted', summary=f'Deleted {kind} {label}.')
        messages.success(self.request, f'Deleted {label}.')
        return response


# --- Template context ----------------------------------------------------------------------------

def branding(request):
    context = {'APP_NAME': settings.APP_NAME, 'SITE_URL': settings.SITE_URL}
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated and request.resolver_match \
            and request.resolver_match.app_name in ('rentals', 'payments', 'maintenance', 'notifications', 'accounts', 'reports', 'audit', 'support', 'announcements'):
        # Lazily imported to keep this module importable before apps are ready.
        from maintenance.models import MaintenanceRequest
        from payments.models import C2BPayment, MpesaAccount

        context['nav_open_requests'] = MaintenanceRequest.objects.filter(
            unit__property__owner=user,
        ).exclude(
            status__in=[MaintenanceRequest.Status.RESOLVED, MaintenanceRequest.Status.CANCELLED],
        ).count()
        from support.models import Complaint

        context['nav_complaints'] = Complaint.objects.filter(owner=user, unread_by_landlord=True).count()
        context['nav_unmatched_c2b'] = C2BPayment.objects.filter(
            account__owner=user, payment__isnull=True,
        ).count()
        account = MpesaAccount.objects.filter(owner=user).first()
        context['nav_mpesa_missing'] = account is None or not account.is_complete()
        from rentals.views import latest_payment_id

        context['live_cursor'] = latest_payment_id(user)
        from announcements.models import PlatformNotice

        context['platform_notices'] = list(PlatformNotice.current_for(user)[:3])
        from accounts.utils import is_verified
        from accounts.views import setup_steps

        context['email_unverified'] = not is_verified(user)
        context['nav_setup_incomplete'] = not all(step['done'] for step in setup_steps(user))
    return context
