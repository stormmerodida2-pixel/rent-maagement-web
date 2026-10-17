"""Helpers for the rentals app: phone numbers, tenant language, billing, rent changes and view mixins."""

import calendar
import csv
import datetime
import io
import re
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps

from django.conf import settings
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import ProtectedError, Sum
from django.shortcuts import redirect
from django.utils import timezone, translation

from audit import utils as audit
from notifications.models import SmsMessage

from .models import Lease, MeterReading, PaymentPlan, PlanInstalment, RentChange, RentCharge, Tenant, Unit


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
def record_water_reading(unit, reading_date, value, recorded_by=None, photo=None):
    """Save a meter reading and bill the consumption since the previous one to the unit's tenant.

    The first reading for a unit is just a baseline. `photo` is an already-prepared image file. Returns the
    MeterReading.
    """
    value = Decimal(value)
    previous = last_reading(unit)
    if previous is not None:
        if reading_date < previous.reading_date:
            raise BillingError(f'{unit.label}: date is before the last reading ({previous.reading_date:%d %b %Y}).')
        if value < previous.value:
            raise BillingError(f'{unit.label}: {value} is lower than the last reading ({previous.value}).')

    reading = MeterReading(unit=unit, reading_date=reading_date, value=value, recorded_by=recorded_by)
    if photo is not None:
        reading.photo.save(photo.name, photo, save=False)
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


def add_tenant_note(lease, author, text, promised_amount=None, promised_by=None):
    """A note on a tenancy (from the landlord or a caretaker), optionally a promise to pay by a date."""
    from .models import TenantNote

    note = TenantNote.objects.create(lease=lease, author=author, text=text, promised_amount=promised_amount,
                                     promised_by=promised_by)
    promise = (f' Promised {"KES " + format(promised_amount, ",.0f") + " " if promised_amount else ""}'
               f'by {promised_by:%d %b}.') if promised_by else ''
    who = 'You' if author.pk == lease.unit.property.owner_id else f'{author.get_full_name() or "Staff"} (staff)'
    audit.record(owner=lease.unit.property.owner, actor=author, lease=lease, action='lease.note_added',
                 summary=f'{who} added a note on {lease.tenant}: "{text[:80]}".{promise}')
    return note


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

def social_links():
    from django.db import OperationalError, ProgrammingError

    from ops.models import SiteSettings

    try:
        return SiteSettings.load().social_links()
    except (OperationalError, ProgrammingError):  # before the first migrate
        return []


def billing_enabled():
    from django.db import OperationalError, ProgrammingError

    from billing.utils import enabled

    try:
        return enabled()
    except (OperationalError, ProgrammingError):  # the billing table doesn't exist yet (first migrate)
        return False


def branding(request):
    context = {'APP_NAME': settings.APP_NAME, 'SITE_URL': settings.SITE_URL, 'SOCIAL_LINKS': social_links(),
               'BILLING_ENABLED': billing_enabled()}
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated and request.resolver_match \
            and request.resolver_match.app_name in ('rentals', 'payments', 'maintenance', 'notifications', 'accounts', 'reports', 'audit', 'support', 'announcements',
                                                 'billing', 'listings'):
        # Lazily imported to keep this module importable before apps are ready.
        from maintenance.models import MaintenanceRequest
        from payments.models import C2BPayment, MpesaAccount

        context['nav_open_requests'] = MaintenanceRequest.objects.filter(
            property__owner=user,
        ).exclude(
            status__in=[MaintenanceRequest.Status.RESOLVED, MaintenanceRequest.Status.CANCELLED],
        ).count()
        from support.models import Complaint

        context['nav_complaints'] = Complaint.objects.filter(owner=user, unread_by_landlord=True).count()
        context['nav_unmatched_c2b'] = C2BPayment.objects.filter(
            account__owner=user, payment__isnull=True,
        ).count()
        account = MpesaAccount.objects.filter(owner=user).first()
        context['nav_mpesa_missing'] = account is None or not account.has_pay_number
        from rentals.views import latest_payment_id

        context['live_cursor'] = latest_payment_id(user)
        from announcements.models import PlatformNotice

        context['platform_notices'] = list(PlatformNotice.current_for(user)[:3])
        from accounts.utils import is_verified

        if not is_verified(user):
            from accounts.models import profile_for

            # Ask them to confirm whichever they have: the email link, or the SMS code for a phone-only account.
            context['verify_prompt'] = 'email' if user.email else ('phone' if profile_for(user).phone else None)
        if context['BILLING_ENABLED']:
            from billing.utils import banner

            context['billing_banner'] = banner(user)
    return context


# --- Spreadsheet import -------------------------------------------------------------------------------

IMPORT_MAX_ROWS = 1000
# Column headings we recognise (case and spaces don't matter). Unit and Rent are required.
IMPORT_COLUMNS = {
    'unit': ['unit', 'unit no', 'house', 'house no', 'room'],
    'type': ['type', 'unit type'],
    'bedrooms': ['bedrooms', 'beds'],
    'rent': ['rent', 'monthly rent', 'rent kes'],
    'tenant': ['tenant name', 'tenant', 'name'],
    'phone': ['phone', 'phone number', 'mpesa phone', 'm-pesa phone'],
    'email': ['email', 'email address'],
    'rent_day': ['rent day', 'due day'],
    'moved_in': ['moved in', 'move-in date', 'move in date', 'start date'],
    'deposit': ['deposit', 'deposit paid'],
    'balance': ['balance owed now', 'balance owed', 'balance', 'arrears'],
    'language': ['language'],
}
IMPORT_TEMPLATE_HEADINGS = ['Unit', 'Type', 'Rent', 'Tenant name', 'Phone', 'Email', 'Rent day',
                            'Moved in', 'Deposit', 'Balance owed now', 'Language']


def unit_type_from_text(text):
    """One of the four unit types from how people write them ("Bedsitter", "2-bedroom", "1BR", "Shop"),
    or None if it isn't clear."""
    t = re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()
    for value, label in Unit.Type.choices:
        if t == label.lower() or t == value.replace('_', ' '):
            return value
    if re.search(r'\b(shop|kiosk|stall|office|store)\b', t):
        return Unit.Type.SHOP
    if re.search(r'\b(single|bedsitter|bedsit|studio)\b', t):
        return Unit.Type.SINGLE
    if re.search(r'\b(2|two)\s*(br|bed|bedroom|bedrooms)\b', t) or re.search(r'\b2br\b', t):
        return Unit.Type.TWO_BEDROOM
    if re.search(r'\b(1|one)\s*(br|bed|bedroom)\b', t) or re.search(r'\b1br\b', t):
        return Unit.Type.ONE_BEDROOM
    return None


class ImportError_(ValueError):
    """The file as a whole can't be read (wrong format, no Unit/Rent columns…)."""


def _heading(value):
    text = re.sub(r'\(.*?\)|\*', '', str(value or '')).strip().lower().replace('_', ' ')
    return re.sub(r'\s+', ' ', text)


def read_spreadsheet(upload):
    """Rows of {field: value} from an .xlsx or .csv upload, using the recognised column headings."""
    name = (upload.name or '').lower()
    if name.endswith('.csv'):
        text = upload.read().decode('utf-8-sig', errors='replace')
        table = list(csv.reader(io.StringIO(text)))
    elif name.endswith(('.xlsx', '.xlsm')):
        try:
            sheet = load_workbook(upload, read_only=True, data_only=True).worksheets[0]
        except Exception as exc:
            raise ImportError_("That file couldn't be opened as an Excel workbook.") from exc
        table = [list(row) for row in sheet.iter_rows(values_only=True)]
    else:
        raise ImportError_('Upload an Excel file (.xlsx) or a CSV file.')
    table = [row for row in table if any(cell not in (None, '') for cell in row)]
    if not table:
        raise ImportError_('The file is empty.')
    lookup = {alias: field for field, aliases in IMPORT_COLUMNS.items() for alias in aliases}
    columns = {i: lookup[_heading(h)] for i, h in enumerate(table[0]) if _heading(h) in lookup}
    missing = [f for f in ('unit', 'rent') if f not in columns.values()]
    if missing:
        raise ImportError_('The first row must have column headings, including "Unit" and "Rent". '
                           'Download the template to see the layout.')
    rows = table[1:]
    if len(rows) > IMPORT_MAX_ROWS:
        raise ImportError_(f'Up to {IMPORT_MAX_ROWS} rows at a time, please.')
    out = []
    for line, row in enumerate(rows, start=2):
        values = {field: row[i] if i < len(row) else None for i, field in columns.items()}
        out.append({'line': line, **{k: _plain_cell(v) for k, v in values.items()}})
    return out


def _plain_cell(value):
    """Cells as JSON-friendly text (dates as YYYY-MM-DD), so a preview can be kept in the session."""
    if value is None:
        return ''
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _money(text):
    text = re.sub(r'(?i)kes|ksh|sh|,|\s', '', text or '')
    if text in ('', '-'):
        return None
    return Decimal(text).quantize(Decimal('0.01'))


def _date(text):
    text = (text or '').strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y', '%d %b %Y', '%d %B %Y', '%d/%m/%y'):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(text)


def check_import(rows, owner, prop):
    """Check every row without saving anything. Each row gets `errors` (empty = will be imported),
    the cleaned values, and a plain description of what will happen."""
    existing = {u.label.lower(): u for u in Unit.objects.filter(property=prop)} if prop else {}
    occupied = {lease.unit_id: lease for lease in Lease.objects.filter(unit__property=prop, is_active=True)
                .select_related('tenant')} if prop else {}
    known_phones = set(Tenant.objects.filter(owner=owner).values_list('phone', flat=True))
    seen, checked = set(), []
    for row in rows:
        errors, clean = [], {'line': row['line']}
        label = row.get('unit', '').strip()
        if not label:
            errors.append('Unit is empty')
        elif label.lower() in seen:
            errors.append(f'Unit {label} appears twice in the file')
        seen.add(label.lower())
        clean['unit'] = label[:50]
        type_text, beds_text = row.get('type', '').strip(), row.get('bedrooms', '').strip()
        clean['type'] = unit_type_from_text(type_text) if type_text else \
            {'0': Unit.Type.SINGLE, '1': Unit.Type.ONE_BEDROOM, '2': Unit.Type.TWO_BEDROOM}.get(
                beds_text.split('.')[0], Unit.Type.SINGLE if not beds_text else None)
        if clean['type'] is None:
            errors.append(f'Type "{type_text or beds_text + " bedrooms"}" should be Single room, One bedroom, '
                          'Two bedroom or Shop')
        for field, label_text, allow_negative in (('rent', 'Rent', False), ('deposit', 'Deposit', False),
                                                   ('balance', 'Balance owed now', True)):
            try:
                amount = _money(row.get(field))
                if amount is not None and amount < 0 and not allow_negative:
                    raise ValueError
                clean[field] = amount
            except (ArithmeticError, ValueError):
                errors.append(f'{label_text} should be an amount like 18000')
                clean[field] = None
        if clean.get('rent') in (None, Decimal('0')) and not any(e.startswith('Rent') for e in errors):
            errors.append('Rent is missing')
        name, phone = row.get('tenant', '').strip(), row.get('phone', '').strip()
        clean['tenant'], clean['email'] = name[:200], row.get('email', '').strip()[:254]
        clean['phone'] = ''
        if name or phone:
            if not name:
                errors.append('Phone given but no tenant name')
            try:
                clean['phone'] = normalize_phone(phone)
            except ValueError:
                errors.append(f'Phone "{phone}" isn\'t a Kenyan mobile number' if phone else 'Tenant has no phone')
        try:
            clean['moved_in'] = _date(row.get('moved_in'))
            if clean['moved_in'] and clean['moved_in'] > datetime.date.today():
                errors.append('Moved in is in the future')
        except ValueError:
            errors.append('Moved in should be a date like 15/03/2024')
            clean['moved_in'] = None
        try:
            day = row.get('rent_day', '')
            clean['rent_day'] = int(Decimal(day)) if day else None
            if clean['rent_day'] is not None and not 1 <= clean['rent_day'] <= 31:
                raise ValueError
        except (ArithmeticError, ValueError):
            errors.append('Rent day should be a day of the month, 1 to 31')
            clean['rent_day'] = None
        clean['language'] = 'sw' if row.get('language', '').strip().lower() in ('sw', 'swahili', 'kiswahili') else 'en'
        unit = existing.get(label.lower())
        if unit and name and unit.pk in occupied:
            errors.append(f'{label} already has a tenant ({occupied[unit.pk].tenant})')
        # What will happen, in words.
        parts = ['Update unit' if unit else 'New unit']
        if name:
            parts.append(f'{name} moves in' + (' (existing contact)' if clean['phone'] in known_phones else ''))
            if clean.get('balance'):
                parts.append(f'owes KES {clean["balance"]:,.0f}' if clean['balance'] > 0
                             else f'KES {-clean["balance"]:,.0f} in credit')
        elif not (unit and unit.pk in occupied):
            parts.append('vacant')
        clean['action'] = ' · '.join(parts)
        clean['errors'] = errors
        checked.append(clean)
    return checked


MAX_BACK_MONTHS = 60  # five years of month-by-month arrears; older than that, enter one total


def rent_months(lease, today=None):
    """Every rent month from when the tenant moved in up to the one now running, oldest first:
    [{'index', 'start', 'end', 'period', 'amount'}]. The amount is the rent plus any service charge."""
    today = today or datetime.date.today()
    months, month = [], lease.start_date.replace(day=1)
    while len(months) < MAX_BACK_MONTHS:
        start = lease.cycle_start(month)
        if start > today:
            break
        if start >= lease.start_date:
            months.append({'index': len(months), 'start': start, 'end': lease.cycle_end(month), 'period': month,
                           'amount': lease.monthly_rent + (lease.service_charge or Decimal('0'))})
        month = (month + datetime.timedelta(days=32)).replace(day=1)
    return months


def bring_forward(lease, balance, deposit_paid, actor, today=None, unpaid_months=None):
    """Start Rentipay for a tenant who already lives there.

    `balance` is everything they owe today, including this month's rent if it was already due (negative =
    in credit); it becomes a "Balance brought forward" line, which reports don't count as new income.
    `deposit_paid` is a deposit they already paid: held for move-out without adding to what they owe.
    This month's rent, if already due, is part of `balance`, so it isn't billed again.
    """
    today = today or datetime.date.today()
    before = (today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)  # dated before Rentipay
    # Month by month: each unpaid (or part-paid) month is its own line, e.g. "Unpaid rent 5 Aug – 4 Sep 2026".
    for month, owed in unpaid_months or []:
        if owed > 0:
            paid = month['amount'] - owed
            RentCharge.objects.create(lease=lease, kind=RentCharge.Kind.OPENING, period=month['period'],
                                      amount=owed, due_date=month['start'],
                                      description=f"Part paid: KES {paid:,.0f} of {month['amount']:,.0f}" if paid > 0 else '')
    if balance:
        RentCharge.objects.create(lease=lease, kind=RentCharge.Kind.OPENING, period=before, amount=balance,
                                  due_date=today, description='Balance brought forward' if balance > 0
                                  else 'Credit brought forward')
    if deposit_paid:
        RentCharge.objects.create(lease=lease, kind=RentCharge.Kind.DEPOSIT, period=before, amount=deposit_paid,
                                  due_date=lease.start_date, description='Deposit')
        RentCharge.objects.create(lease=lease, kind=RentCharge.Kind.OPENING, period=before, amount=-deposit_paid,
                                  due_date=lease.start_date, description='Deposit paid before Rentipay')
    if lease.start_date <= today and lease.cycle_start(today) <= today:
        RentCharge.all_objects.get_or_create(
            lease=lease, kind=RentCharge.Kind.RENT, period=today.replace(day=1),
            defaults={'amount': lease.monthly_rent, 'due_date': lease.cycle_start(today), 'voided_at': timezone.now(),
                      'voided_by': actor, 'void_reason': 'Included in the balance brought forward'},
        )


def unpaid_items(lease):
    """What a tenant still owes, item by item, oldest first: [(charge, amount still unpaid)].

    Payments and credits settle the oldest charges first. A deposit paid before Rentipay settles its own
    deposit line, so it never shows as owed.
    """
    charges = list(lease.charges.order_by('due_date', 'pk'))
    paid_before = sum((-c.amount for c in charges if c.kind == RentCharge.Kind.OPENING and c.amount < 0
                       and c.description == 'Deposit paid before Rentipay'), Decimal('0'))
    pool = lease.total_paid + sum((-c.amount for c in charges if c.amount < 0), Decimal('0')) - paid_before
    items = []
    for charge in charges:
        if charge.amount <= 0:
            continue
        owed = charge.amount
        if charge.kind == RentCharge.Kind.DEPOSIT and paid_before > 0:
            used = min(paid_before, owed)
            paid_before -= used
            owed -= used
        used = min(pool, owed)
        pool -= used
        owed -= used
        if owed > 0:
            items.append((charge, owed))
    return items


@transaction.atomic
def apply_import(checked, owner, prop, actor):
    """Create the units, tenants and tenancies for rows without errors. Returns (units, tenancies) counts."""
    today = datetime.date.today()
    units = tenancies = 0
    for row in checked:
        if row['errors']:
            continue
        unit, _ = Unit.objects.get_or_create(property=prop, label=row['unit'],
                                             defaults={'default_rent': row['rent']})
        unit.default_rent, unit.unit_type = row['rent'], row['type']
        unit.save()
        units += 1
        if not row['tenant']:
            continue
        tenant = Tenant.objects.filter(owner=owner, phone=row['phone']).first() or Tenant.objects.create(
            owner=owner, full_name=row['tenant'], phone=row['phone'], email=row['email'], language=row['language'])
        start = row['moved_in'] or today
        lease = Lease.objects.create(unit=unit, tenant=tenant, start_date=start, monthly_rent=row['rent'],
                                     deposit=row['deposit'] or 0, due_day=row['rent_day'] or start.day)
        tenancies += 1
        bring_forward(lease, row['balance'] or Decimal('0'), row['deposit'] or Decimal('0'), actor, today)
    audit.record(owner=owner, actor=actor, action='property.imported',
                 summary=f'Imported {units} unit(s) and {tenancies} tenant(s) into {prop} from a spreadsheet.')
    return units, tenancies


def import_template():
    """A ready-to-fill .xlsx: headings, two example rows and a sheet of notes."""
    book = Workbook()
    sheet = book.active
    sheet.title = 'Units and tenants'
    sheet.append(IMPORT_TEMPLATE_HEADINGS)
    sheet.append(['A1', 'One bedroom', 18000, 'Wanjiku Kamau', '0712 345 678', '', 5, '01/06/2024', 18000, 3000, 'Kiswahili'])
    sheet.append(['A2', 'Single room', 12000, '', '', '', '', '', '', '', ''])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='0E7A4B')
    for column, width in zip('ABCDEFGHIJK', (8, 14, 10, 22, 16, 24, 10, 12, 10, 16, 11), strict=True):
        sheet.column_dimensions[column].width = width
    types = DataValidation(type='list', formula1='"' + ','.join(label for _, label in Unit.Type.choices) + '"',
                           allow_blank=True, error='Choose Single room, One bedroom, Two bedroom or Shop.')
    sheet.add_data_validation(types)
    types.add(f'B2:B{IMPORT_MAX_ROWS + 1}')
    notes = book.create_sheet('How to fill it in')
    for line in [
        'One row per unit. Delete the two example rows, then add yours.',
        'Unit and Rent are required. Leave the tenant columns empty for a vacant unit.',
        'Type: Single room, One bedroom, Two bedroom or Shop (pick from the list). Empty = Single room.',
        'Phone: the tenant\'s M-Pesa number, e.g. 0712 345 678.',
        'Rent day: the day of the month rent is due (1-31). Empty = the day they moved in.',
        'Moved in: e.g. 01/06/2024. Empty = today.',
        'Deposit: deposit they have ALREADY paid you. It is held for move-out and not added to what they owe.',
        'Balance owed now: everything they owe today, including this month\'s rent if it was already due.',
        '   Use a minus for credit (they paid ahead), e.g. -2000. Rentipay bills from their next rent day.',
        'Language: English or Kiswahili (for their SMS and rent page). Empty = English.',
    ]:
        notes.append([line])
    notes.column_dimensions['A'].width = 110
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


# --- Payment plans for arrears ----------------------------------------------------------------------

def plan_schedule(total, instalment, frequency, first_due):
    """[(due_date, amount)]: equal instalments, the last one smaller if it doesn't divide evenly."""
    dates, amounts, left, due = [], [], total, first_due
    while left > 0 and len(dates) < 104:  # two years of weekly payments at most
        amount = min(instalment, left)
        dates.append(due)
        amounts.append(amount)
        left -= amount
        if frequency == PaymentPlan.Frequency.WEEKLY:
            due += datetime.timedelta(days=7)
        elif frequency == PaymentPlan.Frequency.FORTNIGHTLY:
            due += datetime.timedelta(days=14)
        else:
            nxt = (due.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
            due = nxt.replace(day=min(first_due.day, calendar.monthrange(nxt.year, nxt.month)[1]))
    return list(zip(dates, amounts, strict=True))


@transaction.atomic
def create_plan(lease, *, total, instalment, frequency, first_due, note, actor):
    plan = PaymentPlan.objects.create(lease=lease, total=total, instalment=instalment, frequency=frequency,
                                      first_due=first_due, note=note)
    PlanInstalment.objects.bulk_create(PlanInstalment(plan=plan, due_date=d, amount=a)
                                       for d, a in plan_schedule(total, instalment, frequency, first_due))
    audit.record(owner=lease.unit.property.owner, actor=actor, action='lease.plan_created', lease=lease,
                 summary=f'Payment plan for {lease.tenant}: KES {total:,.0f} as KES {instalment:,.0f} '
                         f'{plan.get_frequency_display().lower()} from {first_due:%d %b %Y}.')
    return plan


def plan_progress(plan, today=None):
    """How a plan is going: what's been paid towards it, each instalment's state, and whether it's on track.

    Paid towards the plan = payments since it started, minus rent and charges added since then that have
    fallen due. Paying the new month's rent plus an instalment therefore moves the plan on by the instalment.
    """
    from payments.models import Payment  # payments imports these models; keep this module importable first

    today = today or datetime.date.today()
    paid = Payment.objects.filter(lease=plan.lease, paid_at__gte=plan.started_at) \
        .aggregate(t=Sum('amount'))['t'] or Decimal('0')
    added = RentCharge.objects.filter(lease=plan.lease, created_at__gte=plan.started_at, due_date__lte=today) \
        .aggregate(t=Sum('amount'))['t'] or Decimal('0')
    towards = min(max(paid - added, Decimal('0')), plan.total)
    rows, running, missed, next_due = [], Decimal('0'), 0, None
    for inst in plan.instalments.all():
        before, running = running, running + inst.amount
        if towards >= running:
            state = 'paid'
        elif inst.due_date < today:
            state = 'missed' if towards <= before else 'part'
            missed += 1
        else:
            state = 'part' if towards > before else ('due' if inst.due_date == today else 'upcoming')
        if state != 'paid' and next_due is None:
            next_due = inst
        rows.append({'instalment': inst, 'state': state})
    return {
        'paid': towards, 'left': plan.total - towards, 'percent': int(towards * 100 / plan.total) if plan.total else 100,
        'rows': rows, 'missed': missed, 'on_track': missed == 0, 'next': next_due,
        'done': towards >= plan.total,
    }


def refresh_plan(plan, today=None):
    """Mark a plan completed once it's cleared. Returns its progress."""
    progress = plan_progress(plan, today)
    if progress['done'] and plan.status == PaymentPlan.Status.ACTIVE:
        plan.status, plan.ended_at = PaymentPlan.Status.COMPLETED, timezone.now()
        plan.save(update_fields=['status', 'ended_at'])
    return progress


def on_track_plan(lease):
    """The lease's active payment plan if the tenant is keeping to it, else None."""
    plan = lease.payment_plans.filter(status=PaymentPlan.Status.ACTIVE).first()
    return plan if plan and plan_progress(plan)['on_track'] else None
