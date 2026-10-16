import datetime

from django.conf import settings
from django.db import models


class BillingAccount(models.Model):
    """What a landlord has paid Rentipay for. Service runs (trial or paid) until `paid_until`."""

    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='billing')
    trial_ends = models.DateField()
    paid_until = models.DateField(help_text='Trial or paid service runs up to (not including) this day.')
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
