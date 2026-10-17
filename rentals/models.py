import calendar
import datetime
import re
import secrets
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from payments.utils import EncryptedTextField, private_storage



class Property(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='properties'
    )
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300, blank=True)
    water_rate = models.DecimalField(
        'water rate (KES per unit)', max_digits=8, decimal_places=2, null=True, blank=True,
        help_text='Price per meter unit (usually m³). Leave blank if you don\'t bill water by meter.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = 'properties'
        ordering = ['name']

    def __str__(self):
        return self.name


def unit_sort_key(label):
    """Sort labels the way people count: A1, A2 … A10, not A1, A10, A2. Numbers are zero-padded."""
    return re.sub(r'\d+', lambda m: m.group().lstrip('0').zfill(8), (label or '').strip().upper())[:200]


class Unit(models.Model):
    class Type(models.TextChoices):
        SINGLE = 'single', 'Single room'
        ONE_BEDROOM = 'one_bedroom', 'One bedroom'
        TWO_BEDROOM = 'two_bedroom', 'Two bedroom'
        SHOP = 'shop', 'Shop'

    BEDROOMS = {Type.SINGLE: 0, Type.ONE_BEDROOM: 1, Type.TWO_BEDROOM: 2, Type.SHOP: 0}

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='units')
    label = models.CharField(max_length=50, help_text='e.g. A1, House 4')
    unit_type = models.CharField('type', max_length=20, choices=Type.choices, default=Type.SINGLE)
    bedrooms = models.PositiveSmallIntegerField(default=0, editable=False)  # follows the type
    # The usual rent for this unit: what new tenants are offered. Each tenancy has its own rent.
    default_rent = models.DecimalField(max_digits=10, decimal_places=2)
    sort_key = models.CharField(max_length=200, default='', editable=False)  # see unit_sort_key

    class Meta:
        ordering = ['property__name', 'sort_key']
        constraints = [
            models.UniqueConstraint(fields=['property', 'label'], name='unique_unit_label_per_property'),
        ]

    def __str__(self):
        return f'{self.property.name} – {self.label}'

    def save(self, *args, **kwargs):
        self.sort_key = unit_sort_key(self.label)
        self.bedrooms = self.BEDROOMS.get(self.unit_type, 0)
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = {*kwargs['update_fields'], 'sort_key', 'bedrooms'}
        super().save(*args, **kwargs)

    @classmethod
    def build(cls, **fields):
        """An unsaved Unit with its derived fields set, for bulk_create (which skips save())."""
        unit = cls(**fields)
        unit.sort_key = unit_sort_key(unit.label)
        unit.bedrooms = cls.BEDROOMS.get(unit.unit_type, 0)
        return unit

    def active_lease(self):
        return self.leases.filter(is_active=True).select_related('tenant').first()


class ActiveManager(models.Manager):
    """Default manager for money records: voided rows are invisible to balances, reports and lists."""

    def get_queryset(self):
        return super().get_queryset().filter(voided_at__isnull=True)


class Voidable(models.Model):
    """Money records are never deleted, only voided with a reason, so the history stays complete."""

    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    void_reason = models.CharField(max_length=200, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    @property
    def is_voided(self):
        return self.voided_at is not None

    def void(self, user, reason):
        self.voided_at = timezone.now()
        self.voided_by = user
        self.void_reason = reason[:200]
        self.save(update_fields=['voided_at', 'voided_by', 'void_reason'])


def new_portal_token():
    return secrets.token_urlsafe(16)


class Tenant(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='tenants'
    )
    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, help_text='M-Pesa number, e.g. 0712345678')
    email = models.EmailField(blank=True)
    # Encrypted at rest (personal data under the Data Protection Act); can't be searched.
    id_number = EncryptedTextField('national ID / passport', blank=True)
    language = models.CharField(
        max_length=2, choices=settings.LANGUAGES, default='en',
        help_text='The language they read best. Their SMS, receipts and rent page use it.',
    )
    portal_token = models.CharField(
        max_length=32, unique=True, default=new_portal_token, editable=False,
        help_text='Secret part of the tenant\'s self-service link.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['full_name']

    def __str__(self):
        return self.full_name

    def clean(self):
        from .utils import normalize_phone  # utils imports these models, so not at the top

        try:
            self.phone = normalize_phone(self.phone)
        except ValueError as exc:
            raise ValidationError({'phone': str(exc)}) from exc

    def save(self, *args, **kwargs):
        from .utils import normalize_phone

        try:
            self.phone = normalize_phone(self.phone)
        except ValueError:
            pass  # clean() reports this in forms; don't block programmatic saves
        super().save(*args, **kwargs)

    def portal_url(self):
        return settings.SITE_URL.rstrip('/') + reverse('rentals:tenant_portal', args=[self.portal_token])

    def reset_portal_token(self):
        self.portal_token = new_portal_token()
        self.save(update_fields=['portal_token'])


BILL_AHEAD_DAYS = 5  # rent is billed this many days before each tenant's rent day


class Lease(models.Model):
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, related_name='leases')
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name='leases')
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    monthly_rent = models.DecimalField(max_digits=10, decimal_places=2)
    deposit = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    service_charge = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0'),
        help_text='Monthly fixed charge billed with rent, e.g. garbage or security.',
    )
    late_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0'),
        help_text='Charged once a month if rent is still unpaid after the grace period. 0 = no late fee.',
    )
    late_fee_grace_days = models.PositiveSmallIntegerField(
        default=3, help_text='Days after the due date before a late fee is added.',
    )
    # The rent day. Each month's rent covers the cycle from this day to the day before it next month
    # (moved in on the 12th: 12 Oct – 11 Nov). 29–31 fall back to the last day of shorter months.
    due_day = models.PositiveSmallIntegerField(
        'rent day', default=5,
        validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text='Day of the month rent is due. Each payment covers a month from this day.',
    )
    is_active = models.BooleanField(default=True)
    # Month-to-month tenancies have no end date until someone gives notice; end_date is then the
    # planned move-out date (billing stops after that month).
    notice_given_on = models.DateField(null=True, blank=True)
    notice_note = models.CharField(max_length=200, blank=True)
    move_out_reminded_on = models.DateField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ['-is_active', 'unit__property__name', 'unit__sort_key']
        constraints = [
            models.UniqueConstraint(
                fields=['unit'], condition=Q(is_active=True), name='one_active_lease_per_unit'
            ),
        ]

    def __str__(self):
        return f'{self.tenant} @ {self.unit}'

    @property
    def moving_out(self):
        """True when notice has been given (or a move-out date set) on a lease that hasn't ended yet."""
        return self.is_active and self.end_date is not None

    @property
    def move_out_overdue(self):
        return self.moving_out and self.end_date < datetime.date.today()

    @property
    def account_number(self):
        """What tenants enter as the Paybill account number. Also sent as the STK AccountReference."""
        return f'LEASE{self.pk}'

    @classmethod
    def find_by_account_number(cls, value, owner=None):
        """Match what a tenant typed ("LEASE12", "lease 12", "Lease-12") to a lease, or return None.

        The LEASE prefix is required: a bare number is too likely to be a house number
        and would silently credit someone else's lease. Pass `owner` to only consider
        that landlord's leases.
        """
        match = re.fullmatch(r'LEASE0*(\d+)', re.sub(r'[^A-Z0-9]', '', (value or '').upper()))
        if not match:
            return None
        leases = cls.objects.filter(pk=int(match[1]))
        if owner is not None:
            leases = leases.filter(unit__property__owner=owner)
        return leases.select_related('tenant').first()

    def clean(self):
        if self.end_date and self.end_date < self.start_date:
            raise ValidationError({'end_date': 'End date cannot be before the start date.'})

    @property
    def total_charged(self):
        return self.charges.aggregate(total=Sum('amount'))['total'] or Decimal('0')

    @property
    def total_paid(self):
        return self.payments.aggregate(total=Sum('amount'))['total'] or Decimal('0')

    @property
    def balance(self):
        """Amount owed. Negative means the tenant has paid in advance."""
        return self.total_charged - self.total_paid

    def ensure_charge_for(self, period):
        """Bill rent (and any service charge) for the month containing `period` if due.

        Idempotent. Returns (rent charge, created).
        """
        period = period.replace(day=1)
        if period < self.start_date.replace(day=1):
            return None, False
        due_date = self.cycle_start(period)
        if self.end_date and due_date > self.end_date:
            return None, False  # this month's cycle would start after they've moved out
        if self.service_charge:
            RentCharge.all_objects.get_or_create(
                lease=self, period=period, kind=RentCharge.Kind.SERVICE,
                defaults={'amount': self.service_charge, 'due_date': due_date},
            )
        rent = self.rent_for(due_date)
        # all_objects: a voided monthly charge means "don't bill this month", not "bill it again".
        result = RentCharge.all_objects.get_or_create(
            lease=self, period=period, kind=RentCharge.Kind.RENT,
            defaults={'amount': rent, 'due_date': due_date},
        )
        self._apply_rent_change(due_date)
        return result

    def pending_rent_change(self):
        return self.rent_changes.filter(applied_at__isnull=True).first()

    def rent_for(self, cycle_start):
        """Rent for the month starting `cycle_start`, counting a planned change that has started by then."""
        change = self.rent_changes.filter(applied_at__isnull=True, effective_from__lte=cycle_start).first()
        return change.new_rent if change else self.monthly_rent

    def _apply_rent_change(self, cycle_start):
        """Once a planned change has been billed, it becomes the lease's rent."""
        change = self.rent_changes.filter(applied_at__isnull=True, effective_from__lte=cycle_start).first()
        if change:
            self.monthly_rent = change.new_rent
            self.save(update_fields=['monthly_rent'])
            change.applied_at = timezone.now()
            change.save(update_fields=['applied_at'])

    def first_rent_day_from(self, day):
        """The first rent day on or after `day` (when a change starting `day` first shows up in billing)."""
        start = self.cycle_start(day.replace(day=1))
        if start >= day:
            return start
        return self.cycle_start((day.replace(day=1) + datetime.timedelta(days=32)).replace(day=1))

    def cycle_start(self, month):
        """The rent day in the month containing `month` (the 31st becomes the 30th, 28th… in short months)."""
        last_day = calendar.monthrange(month.year, month.month)[1]
        return month.replace(day=min(self.due_day, last_day))

    def cycle_end(self, month):
        """Last day covered by the rent due in the month containing `month`: the day before the next rent day."""
        next_month = (month.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        return self.cycle_start(next_month) - datetime.timedelta(days=1)

    def bill_from(self, month):
        """When to bill the rent due in `month`: a few days ahead of the rent day, never before the 1st."""
        return max(month.replace(day=1), self.cycle_start(month) - datetime.timedelta(days=BILL_AHEAD_DAYS))

    def default_move_out(self, today=None):
        """For a notice given today: the end of the first rent month that leaves at least a month's notice."""
        today = today or datetime.date.today()
        month = today.replace(day=1)
        while self.cycle_end(month) < today + datetime.timedelta(days=30):
            month = (month + datetime.timedelta(days=32)).replace(day=1)
        return self.cycle_end(month)

    def apply_late_fee(self, today=None):
        """Add this month's late fee if rent is unpaid past the grace period. Returns the charge or None."""
        today = today or datetime.date.today()
        if not self.late_fee:
            return None
        rent = self.charges.filter(kind=RentCharge.Kind.RENT, period__lte=today).order_by('-period').first()
        if rent is None or today <= rent.due_date + datetime.timedelta(days=self.late_fee_grace_days):
            return None
        if self.balance <= 0:
            return None
        charge, created = RentCharge.all_objects.get_or_create(
            lease=self, period=rent.period, kind=RentCharge.Kind.LATE_FEE,
            defaults={'amount': self.late_fee, 'due_date': today,
                      'description': f'Late fee for {rent.period:%B %Y}'},
        )
        return charge if created else None

    def bill_deposit(self):
        """Charge the deposit once. Returns the charge or None."""
        if not self.deposit or self.charges.filter(kind=RentCharge.Kind.DEPOSIT).exists():
            return None
        return RentCharge.objects.create(
            lease=self, period=self.start_date.replace(day=1), kind=RentCharge.Kind.DEPOSIT,
            amount=self.deposit, due_date=self.start_date, description='Deposit',
        )

    @property
    def deposit_held(self):
        """Deposit billed and not yet applied at move-out."""
        return self.charges.filter(
            kind__in=[RentCharge.Kind.DEPOSIT, RentCharge.Kind.DEPOSIT_APPLIED],
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')


# Descriptions the app writes itself (stored in English) and their translatable forms.
# Descriptions a landlord types in are shown as written.
_FIXED_DESCRIPTIONS = {
    'Deposit': _('Deposit'),
    'Move-out deduction': _('Move-out deduction'),
    'Deposit applied at move-out': _('Deposit applied at move-out'),
    'Deposit refund paid to tenant': _('Deposit refund paid to tenant'),
    'Balance brought forward': _('Balance brought forward'),
    'Credit brought forward': _('Credit brought forward'),
    'Deposit paid before Rentipay': _('Deposit paid before Rentipay'),
}
_WATER_DESCRIPTION = re.compile(r'^Water (?P<used>\S+) units \((?P<start>\S+) → (?P<end>\S+)\) @ KES (?P<rate>\S+)$')
_DEDUCTION_DESCRIPTION = re.compile(r'^Move-out deduction: (?P<reason>.+)$', re.S)
# A month part-paid before Rentipay, entered month by month (the line shows the dates, then this).
_PART_PAID_DESCRIPTION = re.compile(r'^Part paid: KES (?P<paid>[\d,]+) of (?P<total>[\d,]+)$')


def translate_description(text):
    if text in _FIXED_DESCRIPTIONS:
        return str(_FIXED_DESCRIPTIONS[text])
    if m := _WATER_DESCRIPTION.match(text):
        return gettext('Water %(used)s units (%(start)s → %(end)s) @ KES %(rate)s') % m.groupdict()
    if m := _DEDUCTION_DESCRIPTION.match(text):
        return gettext('Move-out deduction: %(reason)s') % m.groupdict()
    return text


class PaymentPlan(models.Model):
    """An agreement to clear arrears in instalments, e.g. KES 45,000 as KES 10,000 every Friday.

    Progress is measured as: payments since the plan started, minus rent and charges that have fallen due
    since then. So paying the new month's rent plus an instalment moves the plan on by the instalment.
    """

    class Frequency(models.TextChoices):
        WEEKLY = 'weekly', 'Every week'
        FORTNIGHTLY = 'fortnightly', 'Every 2 weeks'
        MONTHLY = 'monthly', 'Every month'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name='payment_plans')
    total = models.DecimalField('amount to clear (KES)', max_digits=10, decimal_places=2)
    instalment = models.DecimalField('each payment (KES)', max_digits=10, decimal_places=2)
    frequency = models.CharField(max_length=12, choices=Frequency.choices, default=Frequency.WEEKLY)
    first_due = models.DateField('first payment on')
    note = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
        constraints = [models.UniqueConstraint(fields=['lease'], condition=Q(status='active'),
                                               name='one_active_plan_per_lease')]

    def __str__(self):
        return f'{self.lease.tenant}: KES {self.total:,.0f} as KES {self.instalment:,.0f} {self.get_frequency_display().lower()}'


class PlanInstalment(models.Model):
    plan = models.ForeignKey(PaymentPlan, on_delete=models.CASCADE, related_name='instalments')
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reminded_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['due_date']


class RentChange(models.Model):
    """A planned rent change for a month-to-month lease, starting with the first rent day on or after
    `effective_from`. Applied automatically when that month is billed."""

    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name='rent_changes')
    old_rent = models.DecimalField(max_digits=10, decimal_places=2)
    new_rent = models.DecimalField(max_digits=10, decimal_places=2)
    effective_from = models.DateField()
    notified_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-effective_from', '-pk']
        constraints = [models.UniqueConstraint(fields=['lease'], condition=Q(applied_at__isnull=True),
                                               name='one_pending_rent_change_per_lease')]

    def __str__(self):
        return f'{self.lease}: KES {self.old_rent:,.0f} → {self.new_rent:,.0f} from {self.effective_from}'

    @property
    def starts_on(self):
        return self.lease.first_rent_day_from(self.effective_from)


class RentCharge(Voidable):
    """A line on a tenant's account: rent, utilities, fees, deposit, or a credit (negative amount)."""

    class Kind(models.TextChoices):
        RENT = 'rent', _('Rent')
        SERVICE = 'service', _('Service charge')
        WATER = 'water', _('Water')
        LATE_FEE = 'late_fee', _('Late fee')
        DEPOSIT = 'deposit', _('Deposit')
        DEPOSIT_APPLIED = 'deposit_applied', _('Deposit applied')
        DEDUCTION = 'deduction', _('Move-out deduction')
        REFUND = 'refund', _('Refund paid to tenant')
        OPENING = 'opening', _('Balance brought forward')  # owed (or in credit) before using Rentipay
        OTHER = 'other', _('Other')

    # Kinds billed at most once per lease per month.
    MONTHLY_KINDS = [Kind.RENT, Kind.SERVICE, Kind.LATE_FEE]

    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name='charges')
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.RENT)
    description = models.CharField(max_length=200, blank=True)
    period = models.DateField(help_text='First day of the month being billed.')
    amount = models.DecimalField(max_digits=10, decimal_places=2, help_text='Negative for credits.')
    due_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period', '-created_at']
        verbose_name = 'charge'
        constraints = [
            models.UniqueConstraint(
                fields=['lease', 'period', 'kind'], condition=Q(kind__in=['rent', 'service', 'late_fee']),
                name='one_monthly_charge_per_kind',
            ),
        ]

    def __str__(self):
        return f'{self.lease} – {self.label}'

    @property
    def label(self):
        """What the charge is for, in the active language (tenants may read Kiswahili)."""
        part = _PART_PAID_DESCRIPTION.match(self.description or '')
        if self.kind == self.Kind.OPENING and (part or not self.description):
            # A month unpaid (or part-paid) before Rentipay, entered month by month.
            end = self.lease.cycle_end(self.period)
            text = gettext('Unpaid rent %(start)s – %(end)s') % {'start': date_format(self.due_date, 'j M'),
                                                                 'end': date_format(end, 'j M Y')}
            if part:
                text += ' ' + gettext('(KES %(paid)s of %(total)s paid)') % part.groupdict()
            return text
        if self.description:
            return translate_description(self.description)
        if self.kind in (self.Kind.RENT, self.Kind.SERVICE) and self.due_date.day != 1:
            # A rent-day cycle, e.g. "Rent 12 Oct – 11 Nov 2026".
            end = self.lease.cycle_end(self.period)
            dates = {'start': date_format(self.due_date, 'j M'), 'end': date_format(end, 'j M Y')}
            if self.kind == self.Kind.RENT:
                return gettext('Rent %(start)s – %(end)s') % dates
            return gettext('%(kind)s %(start)s – %(end)s') % {'kind': self.get_kind_display(), **dates}
        month = date_format(self.period, 'F Y')
        if self.kind == self.Kind.RENT:
            return gettext('Rent for %(month)s') % {'month': month}
        return gettext('%(kind)s for %(month)s') % {'kind': self.get_kind_display(), 'month': month}

    @property
    def is_overdue(self):
        return self.due_date < datetime.date.today()


class MeterReading(Voidable):
    """A water meter reading. Each reading after the first bills the consumption since the last one.

    Voided readings are ignored, so the reading before a voided one becomes the baseline again.
    """

    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, related_name='meter_readings')
    reading_date = models.DateField()
    value = models.DecimalField(max_digits=12, decimal_places=2)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    photo = models.ImageField(upload_to='meters/%Y/%m/', storage=private_storage, blank=True)
    charge = models.OneToOneField(
        RentCharge, on_delete=models.SET_NULL, null=True, blank=True, related_name='meter_reading',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-reading_date', '-created_at']

    def __str__(self):
        return f'{self.unit} – {self.value} on {self.reading_date}'


class TenantNote(models.Model):
    """A note on a tenancy from the landlord or a caretaker, e.g. "Promised to pay KES 10,000 by Friday"."""

    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name='notes')
    text = models.CharField(max_length=300)
    promised_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    promised_by = models.DateField('promised to pay by', null=True, blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.text[:50]

    def promise_missed(self, today=None):
        """The promise date has passed and the tenant still owes."""
        today = today or timezone.localdate()
        return bool(self.promised_by and self.promised_by < today and self.lease.balance > 0)

