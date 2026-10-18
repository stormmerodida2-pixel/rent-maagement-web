import datetime
import secrets

from django.conf import settings
from django.core.cache import cache
from django.db import models

from payments.utils import EncryptedTextField


def new_callback_token():
    return secrets.token_urlsafe(24)


class BillingSettings(models.Model):
    """How Rentipay charges landlords, set by an administrator in Billing overview. One row."""

    class Environment(models.TextChoices):
        PRODUCTION = 'production', 'Live'
        SANDBOX = 'sandbox', 'Sandbox (testing)'

    enabled = models.BooleanField('billing is on', default=False)
    price_per_unit = models.PositiveIntegerField('price per occupied unit a month (KES)', default=75)
    free_units = models.PositiveIntegerField('free units', default=5,
                                             help_text='Every landlord\'s first units are free.')
    trial_days = models.PositiveIntegerField('free trial (days)', default=30)
    grace_days = models.PositiveIntegerField('days before an unpaid account becomes read-only', default=7)
    till = models.CharField('your M-Pesa Till number', max_length=20, blank=True,
                            help_text='Landlords pay here from Lipa na M-Pesa → Buy Goods and Services.')
    notify_email = models.EmailField('email me about payments at', blank=True,
                                     help_text='When a landlord enters an M-Pesa code to check. Empty = administrators\' emails.')
    # Optional: M-Pesa PIN prompts to the Till (its own Daraja app).
    mpesa_environment = models.CharField('Daraja app type', max_length=10, choices=Environment.choices,
                                         default=Environment.PRODUCTION)
    mpesa_store_number = models.CharField('Head Office / Store number', max_length=20, blank=True)
    mpesa_consumer_key = EncryptedTextField('consumer key', blank=True)
    mpesa_consumer_secret = EncryptedTextField('consumer secret', blank=True)
    mpesa_passkey = EncryptedTextField('passkey', blank=True)
    callback_token = models.CharField(max_length=40, default=new_callback_token, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    CACHE_KEY = 'billing:settings'
    # Read on every page, so kept in the cache; the secret keys are left out and read from the database.
    PUBLIC = ['enabled', 'price_per_unit', 'free_units', 'trial_days', 'grace_days', 'till', 'notify_email',
              'mpesa_environment', 'mpesa_store_number']

    class Meta:
        verbose_name = verbose_name_plural = 'billing settings'

    def __str__(self):
        return 'Billing settings'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @classmethod
    def public(cls):
        values = cache.get(cls.CACHE_KEY)
        if values is None:
            row = cls.load()
            values = {name: getattr(row, name) for name in cls.PUBLIC}
            cache.set(cls.CACHE_KEY, values, 300)
        return values

    def prompts_ready(self):
        return all([self.till, self.mpesa_store_number, self.mpesa_consumer_key, self.mpesa_consumer_secret,
                    self.mpesa_passkey])


class BillingAccount(models.Model):
    """What a landlord has paid Rentipay for. Service runs (trial or paid) until `paid_until`."""

    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='billing')
    trial_ends = models.DateField()
    paid_until = models.DateField(help_text='Trial or paid service runs up to (not including) this day.')
    free_months = models.PositiveSmallIntegerField(default=0, help_text='Earned by referring landlords: each one '
                                                                        'makes a coming monthly bill free.')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Billing for {self.owner}'


class Invoice(models.Model):
    """One month of Rentipay for one landlord, billed in advance."""

    class Status(models.TextChoices):
        OPEN = 'open', 'Due'
        PAID = 'paid', 'Paid'
        VOID = 'void', 'Cancelled'

    class Method(models.TextChoices):
        PROMPT = 'prompt', 'M-Pesa prompt'
        TILL = 'till', 'M-Pesa Till'
        MANUAL = 'manual', 'Recorded by admin'
        FREE = 'free', 'Nothing to pay'

    account = models.ForeignKey(BillingAccount, on_delete=models.CASCADE, related_name='invoices')
    period_start = models.DateField()
    period_end = models.DateField(help_text='First day after the period.')
    units = models.PositiveIntegerField(help_text='Occupied units when the bill was made.')
    free_units = models.PositiveIntegerField()
    price_per_unit = models.PositiveIntegerField()
    amount = models.PositiveIntegerField(help_text='KES')
    due_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    paid_at = models.DateTimeField(null=True, blank=True)
    method = models.CharField(max_length=10, choices=Method.choices, blank=True)
    reference = models.CharField('M-Pesa code', max_length=20, blank=True)
    # The landlord says they paid to the Till: their code, waiting for you to check it.
    claimed_reference = models.CharField(max_length=20, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    checkout_request_id = models.CharField(max_length=100, blank=True)  # last M-Pesa prompt
    sent_at = models.DateTimeField(null=True, blank=True)
    overdue_notice_at = models.DateTimeField(null=True, blank=True)
    locked_notice_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period_start']
        constraints = [
            models.UniqueConstraint(fields=['account', 'period_start'], condition=~models.Q(status='void'),
                                    name='one_invoice_per_period'),
            models.UniqueConstraint(fields=['reference'], condition=~models.Q(reference=''),
                                    name='unique_invoice_reference'),
        ]

    def __str__(self):
        return f'{self.number} · KES {self.amount:,}'

    @property
    def number(self):
        return f'RP-{self.pk:05d}'

    @property
    def period_last(self):
        return self.period_end - datetime.timedelta(days=1)

    @property
    def billed_units(self):
        return max(self.units - self.free_units, 0)
