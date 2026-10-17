import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from rentals.models import Lease, Voidable

from .utils import EncryptedTextField, private_storage


def new_callback_token():
    return secrets.token_urlsafe(24)


class MpesaAccount(models.Model):
    """A landlord's own Paybill/Till, so rent goes straight to them.

    The Paybill/Till number alone is enough to start: tenants are told where to pay, and payments come in
    from uploaded M-Pesa statements. Adding the landlord's Daraja app keys makes payments appear instantly
    and enables M-Pesa PIN prompts.
    """

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
        'business short code', max_length=20, blank=True,
        help_text='Your Paybill number. For a Till, the Head Office / Store number used for M-Pesa prompts.',
    )
    till_number = models.CharField(max_length=20, blank=True, help_text='The Buy Goods till number tenants pay to.')
    consumer_key = EncryptedTextField(blank=True)
    consumer_secret = EncryptedTextField(blank=True)
    passkey = EncryptedTextField(blank=True, help_text='Lipa Na M-Pesa Online passkey issued by Safaricom.')
    # Secret path segment in this account's webhook URLs; identifies which landlord a callback is for.
    callback_token = models.CharField(max_length=40, unique=True, default=new_callback_token, editable=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    c2b_registered_at = models.DateTimeField('Paybill URLs registered', null=True, blank=True)
    statement_imported_at = models.DateTimeField('last M-Pesa statement upload', null=True, blank=True)
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
        if self.payment_type == self.PaymentType.PAYBILL and not self.shortcode:
            raise ValidationError({'shortcode': 'Enter your Paybill number.'})

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
        """Daraja is connected: payments arrive instantly and M-Pesa PIN prompts can be sent."""
        return all([self.shortcode, self.consumer_key, self.consumer_secret, self.passkey]) and (
            not self.is_till or self.till_number
        )

    @property
    def has_pay_number(self):
        """Tenants can be told where to pay (enough for statement uploads)."""
        return bool(self.pay_number)


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
    # Who entered a cash or bank payment (the landlord or a caretaker); empty for M-Pesa, which records itself.
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')

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
    """A payment a tenant made directly to the Paybill/Till: reported live by Safaricom's C2B confirmation
    URL, or read from an M-Pesa statement the landlord uploaded."""

    class Source(models.TextChoices):
        LIVE = 'live', 'Safaricom (live)'
        STATEMENT = 'statement', 'M-Pesa statement'

    source = models.CharField(max_length=10, choices=Source.choices, default=Source.LIVE)
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


class CashHandover(models.Model):
    """Cash a caretaker passed on to the landlord (in person, banked or sent by M-Pesa).

    What a caretaker is holding = cash payments they recorded − handovers the landlord confirmed.
    """

    class Method(models.TextChoices):
        IN_PERSON = 'in_person', 'Handed over in person'
        BANKED = 'banked', 'Banked'
        MPESA = 'mpesa', 'Sent by M-Pesa'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Waiting for confirmation'
        CONFIRMED = 'confirmed', 'Confirmed'
        DISPUTED = 'disputed', 'Not received'

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='cash_handovers')
    staff = models.ForeignKey('accounts.StaffMember', on_delete=models.PROTECT, related_name='handovers')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.IN_PERSON)
    reference = models.CharField(max_length=50, blank=True, help_text='Bank slip or M-Pesa code.')
    note = models.CharField(max_length=200, blank=True)
    handed_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-handed_at']

    def __str__(self):
        return f'KES {self.amount:,.0f} from {self.staff} ({self.get_status_display()})'


class StaffExpense(models.Model):
    """Money a caretaker spent from the rent cash they hold (a plumber, a tap…). Only once the landlord
    approves it does it reduce what they're holding."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Waiting for approval'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Not approved'

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='staff_expenses')
    staff = models.ForeignKey('accounts.StaffMember', on_delete=models.PROTECT, related_name='expenses')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.CharField('what for', max_length=200)
    unit = models.ForeignKey('rentals.Unit', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    receipt = models.ImageField(upload_to='receipts/%Y/%m/', storage=private_storage, blank=True)
    spent_on = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    reject_reason = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-spent_on', '-created_at']

    def __str__(self):
        return f'KES {self.amount:,.0f}: {self.description}'



class Expense(Voidable):
    """Money a landlord spent on their properties: repairs, caretaker pay, water, rates…

    Rent collected minus expenses is what the landlord made (Reports). Monthly costs can repeat by themselves;
    approved caretaker spending becomes an expense automatically.
    """

    class Category(models.TextChoices):
        REPAIRS = 'repairs', 'Repairs & maintenance'
        STAFF = 'staff', 'Caretaker & staff pay'
        WATER = 'water', 'Water bill'
        ELECTRICITY = 'electricity', 'Electricity'
        SECURITY = 'security', 'Security'
        CLEANING = 'cleaning', 'Cleaning & garbage'
        RATES = 'rates', 'Land rates & licences'
        INSURANCE = 'insurance', 'Insurance'
        AGENT = 'agent', 'Agent & management fees'
        OTHER = 'other', 'Other'

    class Method(models.TextChoices):
        CASH = 'cash', 'Cash'
        MPESA = 'mpesa', 'M-Pesa'
        BANK = 'bank', 'Bank'
        STAFF_CASH = 'staff_cash', "From caretaker's rent cash"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='expenses')
    property = models.ForeignKey('rentals.Property', on_delete=models.PROTECT, null=True, blank=True,
                                 related_name='expenses', help_text='Leave empty for costs shared by all properties.')
    unit = models.ForeignKey('rentals.Unit', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.REPAIRS)
    description = models.CharField('what for', max_length=200)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_on = models.DateField(default=timezone.localdate)
    method = models.CharField('paid by', max_length=12, choices=Method.choices, default=Method.MPESA)
    reference = models.CharField(max_length=50, blank=True, help_text='M-Pesa code, cheque or invoice number.')
    receipt = models.ImageField(upload_to='expenses/%Y/%m/', storage=private_storage, blank=True)
    repeats_monthly = models.BooleanField('repeat every month', default=False)
    repeat_of = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='repeats')
    repair = models.ForeignKey('maintenance.MaintenanceRequest', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='expenses')
    staff_expense = models.OneToOneField(StaffExpense, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name='expense')
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-paid_on', '-created_at']

    def __str__(self):
        return f'KES {self.amount:,.0f}: {self.description}'
