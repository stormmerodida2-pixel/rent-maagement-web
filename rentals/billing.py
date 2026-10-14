"""Billing operations beyond monthly rent: water from meter readings and move-out settlement."""

import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from .models import MeterReading, RentCharge


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
