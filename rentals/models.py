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

from .phone import normalize_phone


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


class Unit(models.Model):
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='units')
    label = models.CharField(max_length=50, help_text='e.g. A1, House 4')
    bedrooms = models.PositiveSmallIntegerField(default=1)
    default_rent = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ['property__name', 'label']
        constraints = [
            models.UniqueConstraint(fields=['property', 'label'], name='unique_unit_label_per_property'),
        ]

    def __str__(self):
        return f'{self.property.name} – {self.label}'

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
    id_number = models.CharField('national ID / passport', max_length=50, blank=True)
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
        try:
            self.phone = normalize_phone(self.phone)
        except ValueError as exc:
            raise ValidationError({'phone': str(exc)})

    def save(self, *args, **kwargs):
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
    due_day = models.PositiveSmallIntegerField(
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(28)],
        help_text='Day of the month rent is due (1–28).',
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-is_active', 'unit__property__name', 'unit__label']
        constraints = [
            models.UniqueConstraint(
                fields=['unit'], condition=Q(is_active=True), name='one_active_lease_per_unit'
            ),
        ]

    def __str__(self):
        return f'{self.tenant} @ {self.unit}'

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
        if self.end_date and period > self.end_date:
            return None, False
        last_day = calendar.monthrange(period.year, period.month)[1]
        due_date = period.replace(day=min(self.due_day, last_day))
        if self.service_charge:
            RentCharge.all_objects.get_or_create(
                lease=self, period=period, kind=RentCharge.Kind.SERVICE,
                defaults={'amount': self.service_charge, 'due_date': due_date},
            )
        # all_objects: a voided monthly charge means "don't bill this month", not "bill it again".
        return RentCharge.all_objects.get_or_create(
            lease=self, period=period, kind=RentCharge.Kind.RENT,
            defaults={'amount': self.monthly_rent, 'due_date': due_date},
        )

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


class RentCharge(Voidable):
    """A line on a tenant's account: rent, utilities, fees, deposit, or a credit (negative amount)."""

    class Kind(models.TextChoices):
        RENT = 'rent', 'Rent'
        SERVICE = 'service', 'Service charge'
        WATER = 'water', 'Water'
        LATE_FEE = 'late_fee', 'Late fee'
        DEPOSIT = 'deposit', 'Deposit'
        DEPOSIT_APPLIED = 'deposit_applied', 'Deposit applied'
        DEDUCTION = 'deduction', 'Move-out deduction'
        REFUND = 'refund', 'Refund paid to tenant'
        OTHER = 'other', 'Other'

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
        if self.description:
            return self.description
        if self.kind == self.Kind.RENT:
            return f'Rent for {self.period:%B %Y}'
        return f'{self.get_kind_display()} for {self.period:%B %Y}'

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
    charge = models.OneToOneField(
        RentCharge, on_delete=models.SET_NULL, null=True, blank=True, related_name='meter_reading',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-reading_date', '-created_at']

    def __str__(self):
        return f'{self.unit} – {self.value} on {self.reading_date}'
