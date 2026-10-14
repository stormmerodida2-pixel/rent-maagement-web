import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from rentals.models import Lease, Voidable

from .fields import EncryptedTextField


def new_callback_token():
    return secrets.token_urlsafe(24)


class MpesaAccount(models.Model):
    """A landlord's own Daraja app and Paybill/Till, so rent goes straight to them."""

    class Environment(models.TextChoices):
        SANDBOX = 'sandbox', 'Sandbox (testing)'
        PRODUCTION = 'production', 'Production (live money)'

    class PaymentType(models.TextChoices):
        PAYBILL = 'paybill', 'Paybill'
        TILL = 'till', 'Till (Buy Goods)'

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='mpesa_account'
    )
    environment = models.CharField(max_length=10, choices=Environment.choices, default=Environment.SANDBOX)
    payment_type = models.CharField(max_length=10, choices=PaymentType.choices, default=PaymentType.PAYBILL)
    shortcode = models.CharField(
        'business short code', max_length=20,
        help_text='Your Paybill number. For a Till, the Head Office / Store number used for STK Push.',
    )
    till_number = models.CharField(max_length=20, blank=True, help_text='The Buy Goods till number tenants pay to.')
    consumer_key = EncryptedTextField()
    consumer_secret = EncryptedTextField()
    passkey = EncryptedTextField(help_text='Lipa Na M-Pesa Online passkey issued by Safaricom.')
    # Secret path segment in this account's webhook URLs; identifies which landlord a callback is for.
    callback_token = models.CharField(max_length=40, unique=True, default=new_callback_token, editable=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    c2b_registered_at = models.DateTimeField('Paybill URLs registered', null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'M-Pesa account'

    def __str__(self):
        return f'{self.get_payment_type_display()} {self.pay_number} ({self.owner})'

    def clean(self):
        if self.payment_type == self.PaymentType.TILL and not self.till_number:
            raise ValidationError({'till_number': 'Enter the till number tenants pay to.'})

    @property
    def is_till(self):
        return self.payment_type == self.PaymentType.TILL

    @property
    def pay_number(self):
        """The number tenants pay to from the M-Pesa menu."""
        return self.till_number if self.is_till else self.shortcode

    @property
    def transaction_type(self):
        return 'CustomerBuyGoodsOnline' if self.is_till else 'CustomerPayBillOnline'

    def is_complete(self):
        return all([self.shortcode, self.consumer_key, self.consumer_secret, self.passkey]) and (
            not self.is_till or self.till_number
        )


class Payment(Voidable):
    class Method(models.TextChoices):
        MPESA = 'mpesa', _('M-Pesa')
        CASH = 'cash', _('Cash')
        BANK = 'bank', _('Bank transfer')

    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.MPESA)
    reference = models.CharField(
        max_length=50, blank=True, help_text='M-Pesa receipt number or bank reference.'
    )
    paid_at = models.DateTimeField(default=timezone.now)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-paid_at']
        constraints = [
            models.UniqueConstraint(
                fields=['method', 'reference'],
                condition=~models.Q(reference='') & models.Q(voided_at__isnull=True),
                name='unique_payment_reference_per_method',
            ),
        ]

    def __str__(self):
        return f'KES {self.amount} – {self.lease.tenant} ({self.get_method_display()})'


class MpesaTransaction(models.Model):
    """An STK Push request and its outcome as reported by Safaricom's callback."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SUCCESS = 'success', 'Success'
        FAILED = 'failed', 'Failed'

    account = models.ForeignKey(
        MpesaAccount, on_delete=models.PROTECT, null=True, blank=True, related_name='stk_transactions'
    )
    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name='mpesa_transactions')
    phone = models.CharField(max_length=12)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    merchant_request_id = models.CharField(max_length=100, blank=True)
    checkout_request_id = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    result_code = models.IntegerField(null=True, blank=True)
    result_desc = models.CharField(max_length=255, blank=True)
    receipt_number = models.CharField(max_length=50, blank=True)
    raw_callback = models.JSONField(null=True, blank=True)
    payment = models.OneToOneField(
        Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name='mpesa_transaction'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.checkout_request_id} ({self.status})'


class C2BPayment(models.Model):
    """A payment a tenant made directly to the Paybill/Till, as reported by the C2B confirmation URL."""

    account = models.ForeignKey(
        MpesaAccount, on_delete=models.PROTECT, null=True, blank=True, related_name='c2b_payments'
    )
    trans_id = models.CharField('M-Pesa receipt', max_length=50, unique=True)
    trans_type = models.CharField(max_length=50, blank=True)
    trans_time = models.DateTimeField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    shortcode = models.CharField(max_length=20, blank=True)
    bill_ref = models.CharField('account number entered', max_length=100, blank=True)
    msisdn = models.CharField('phone', max_length=100, blank=True)
    payer_name = models.CharField(max_length=200, blank=True)
    raw = models.JSONField()
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    held_for_review = models.BooleanField(
        default=False,
        help_text="Came from an address that isn't Safaricom's, so it wasn't credited automatically. "
                  'Check the M-Pesa statement before assigning it.',
    )
    lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, null=True, blank=True, related_name='c2b_payments',
        help_text='Set this to assign an unmatched payment to a lease.',
    )
    payment = models.OneToOneField(
        Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name='c2b_payment'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-trans_time']
        verbose_name = 'Paybill payment'

    def __str__(self):
        return f'{self.trans_id} – KES {self.amount}'
