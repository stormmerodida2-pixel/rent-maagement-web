import datetime

from django import forms
from django.utils import timezone

from rentals.models import Lease

from .models import Expense, MpesaAccount, Payment

# Safaricom's published sandbox test credentials for Lipa Na M-Pesa Online.
SANDBOX_SHORTCODE = '174379'
SANDBOX_PASSKEY = 'bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919'
SECRET_FIELDS = ['consumer_key', 'consumer_secret', 'passkey']


class StkPushForm(forms.Form):
    amount = forms.DecimalField(min_value=1, max_digits=10, decimal_places=2)
    phone = forms.CharField(max_length=20, required=False, help_text="Defaults to the tenant's phone.")


class MpesaAccountForm(forms.ModelForm):
    """Step 1 (required): where tenants pay. Step 2 (optional): the Daraja app's keys.

    Secrets are never rendered back; leaving one blank keeps the saved value.
    """

    PAY_FIELDS = ['payment_type', 'shortcode', 'till_number']
    DARAJA_FIELDS = ['environment', 'store_number', 'consumer_key', 'consumer_secret', 'passkey']

    store_number = forms.CharField(
        max_length=20, required=False, label='Head Office / Store number',
        help_text='Tills only: the store number linked to your Daraja app. Safaricom sends prompts from it.')

    class Meta:
        model = MpesaAccount
        fields = ['environment', 'payment_type', 'shortcode', 'till_number', 'consumer_key', 'consumer_secret', 'passkey']
        labels = {'payment_type': 'Tenants pay to my', 'shortcode': 'Paybill number',
                  'environment': 'Daraja app type'}
        widgets = {name: forms.PasswordInput(render_value=False) for name in SECRET_FIELDS}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original = {name: getattr(self.instance, name) for name in self.Meta.fields} if self.instance.pk else {}
        self.saved_secrets = {}
        for name in SECRET_FIELDS:
            saved = getattr(self.instance, name, '') if self.instance.pk else ''
            if saved:
                self.saved_secrets[name] = saved
                self.fields[name].required = False
                self.fields[name].widget.attrs['placeholder'] = '•••••••• saved. Leave blank to keep.'
        self.fields['shortcode'].help_text = 'The number tenants enter under Lipa na M-Pesa → Pay Bill.'
        if self.instance.pk and self.instance.is_till:
            self.initial.setdefault('store_number', self.instance.shortcode)
        self.fields['passkey'].help_text = (
            f'Live: from Safaricom\'s go-live email. Sandbox testing: short code {SANDBOX_SHORTCODE} '
            f'with passkey {SANDBOX_PASSKEY}')

    def pay_fields(self):
        return [self[name] for name in self.PAY_FIELDS]

    def daraja_fields(self):
        return [self[name] for name in self.DARAJA_FIELDS]

    def clean(self):
        cleaned = super().clean()
        for name, saved in self.saved_secrets.items():
            if not cleaned.get(name):
                cleaned[name] = saved
        for name in ['shortcode', 'till_number', *SECRET_FIELDS]:
            if cleaned.get(name):
                cleaned[name] = cleaned[name].strip()
        if cleaned.get('payment_type') == MpesaAccount.PaymentType.TILL:
            cleaned['shortcode'] = (cleaned.get('store_number') or '').strip()
        given = [name for name in SECRET_FIELDS if cleaned.get(name)]
        if given and len(given) < len(SECRET_FIELDS):
            for name in SECRET_FIELDS:
                if not cleaned.get(name):
                    self.add_error(name, 'Fill in all three Daraja keys, or leave all three empty to connect '
                                         'Daraja later.')
        if given and cleaned.get('payment_type') == MpesaAccount.PaymentType.TILL and not cleaned.get('shortcode'):
            self.add_error('store_number', 'For a Till, enter the Head Office / Store number linked to Daraja.')
        return cleaned

    def save(self, commit=True):
        account = super().save(commit=False)
        # Credentials changed, so earlier checks no longer apply.
        if any(getattr(account, name) != self.original.get(name) for name in self.Meta.fields):
            account.verified_at = None
            account.last_error = ''
        if commit:
            account.save()
        return account


class ManualPaymentForm(forms.ModelForm):
    """Cash or bank payment entered by the landlord. M-Pesa payments are recorded automatically."""

    class Meta:
        model = Payment
        fields = ['lease', 'amount', 'method', 'reference', 'paid_at', 'notes']
        labels = {'amount': 'Amount (KES)', 'paid_at': 'Date received'}
        widgets = {'paid_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M')}

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['lease'].queryset = (
            Lease.objects.filter(unit__property__owner=owner).select_related('tenant', 'unit__property')
        )
        self.fields['lease'].empty_label = 'Choose a tenant'
        self.fields['method'].choices = [
            (value, label) for value, label in Payment.Method.choices if value != Payment.Method.MPESA
        ]
        self.fields['amount'].min_value = 1
        self.fields['reference'].help_text = 'Bank reference or receipt number (optional).'
        self.initial.setdefault('paid_at', timezone.localtime().replace(second=0, microsecond=0))


class AssignC2BForm(forms.Form):
    lease = forms.ModelChoiceField(queryset=Lease.objects.none(), empty_label='Choose the tenant who paid')

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['lease'].queryset = (
            Lease.objects.filter(unit__property__owner=owner).select_related('tenant', 'unit__property')
        )


class VoidForm(forms.Form):
    reason = forms.CharField(
        max_length=200, label='Why are you voiding this?',
        help_text='Kept in the activity log, e.g. "Entered twice" or "Wrong amount, re-entered".',
    )


class MovePaymentForm(forms.Form):
    """Move an M-Pesa payment to the tenant who actually paid. M-Pesa money is real, so it's moved, never voided."""

    lease = forms.ModelChoiceField(queryset=Lease.objects.none(), label='Move to', empty_label='Choose the tenant who paid')
    reason = forms.CharField(max_length=200, help_text='e.g. "Paid for her sister in B2".')

    def __init__(self, *args, owner, exclude_lease, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['lease'].queryset = (
            Lease.objects.filter(unit__property__owner=owner).exclude(pk=exclude_lease.pk)
            .select_related('tenant', 'unit__property')
        )


class StatementUploadForm(forms.Form):
    file = forms.FileField(label='M-Pesa statement (Excel or CSV)')
    send_receipts = forms.BooleanField(
        required=False, initial=False, label='Text each tenant an SMS receipt',
        help_text='Leave unticked for older payments, so tenants don\'t get receipts for things they paid weeks ago.',
    )



class HandoverForm(forms.Form):
    """Cash passed on by a caretaker. The landlord's version also picks which caretaker."""

    staff = forms.ModelChoiceField(queryset=None, label='From', empty_label='Choose a caretaker')
    amount = forms.DecimalField(label='Amount (KES)', min_value=1, max_digits=10, decimal_places=2,
                                widget=forms.NumberInput(attrs={'inputmode': 'numeric', 'step': '1'}))
    method = forms.ChoiceField(label='How', choices=[])
    reference = forms.CharField(label='Bank slip or M-Pesa code', max_length=50, required=False)
    note = forms.CharField(max_length=200, required=False)

    def __init__(self, *args, owner=None, **kwargs):
        from accounts.models import StaffMember

        from .models import CashHandover

        super().__init__(*args, **kwargs)
        self.fields['method'].choices = CashHandover.Method.choices
        if owner is None:  # a caretaker handing over their own cash
            del self.fields['staff']
        else:
            self.fields['staff'].queryset = StaffMember.objects.filter(owner=owner).select_related('user')

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('method') in ('banked', 'mpesa') and not cleaned.get('reference'):
            self.add_error('reference', 'Add the bank slip number or M-Pesa code, so it can be checked.')
        return cleaned


class ExpenseForm(forms.ModelForm):
    """Something the landlord paid for. A receipt photo is optional and stays private."""

    receipt_photo = forms.ImageField(label='Receipt photo (optional)', required=False,
                                     widget=forms.ClearableFileInput(attrs={'accept': 'image/*'}))

    class Meta:
        model = Expense
        fields = ['category', 'description', 'amount', 'paid_on', 'property', 'unit', 'method', 'reference',
                  'repeats_monthly']
        labels = {'amount': 'Amount (KES)', 'paid_on': 'Date paid', 'property': 'Property', 'unit': 'Unit (optional)'}
        widgets = {'paid_on': forms.DateInput(attrs={'type': 'date'}),
                   'amount': forms.NumberInput(attrs={'inputmode': 'decimal', 'step': '1'}),
                   'description': forms.TextInput(attrs={'placeholder': "e.g. Peter's salary, KPLC tokens, roof repair"})}
        help_texts = {'repeats_monthly': "For costs you pay every month, like the caretaker's salary: it's added "
                                         'again on the same day each month until you stop it.'}

    def __init__(self, *args, owner, **kwargs):
        from rentals.models import Property, Unit

        super().__init__(*args, **kwargs)
        self.owner = owner
        self.fields['property'].queryset = Property.objects.filter(owner=owner)
        self.fields['property'].empty_label = 'All properties (shared cost)'
        self.fields['unit'].queryset = Unit.objects.filter(property__owner=owner).select_related('property')
        self.fields['unit'].empty_label = 'Not for one unit'
        self.fields['method'].choices = [c for c in Expense.Method.choices if c[0] != Expense.Method.STAFF_CASH]
        if self.instance.pk:
            del self.fields['repeats_monthly']

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= 0:
            raise forms.ValidationError('Enter the amount you paid.')
        return amount

    def clean(self):
        cleaned = super().clean()
        unit, prop = cleaned.get('unit'), cleaned.get('property')
        if unit and prop and unit.property_id != prop.pk:
            self.add_error('unit', f'{unit.label} is not at {prop}.')
        if unit and not prop:
            cleaned['property'] = unit.property  # a unit's cost belongs to its property
            self.instance.property = unit.property
        if cleaned.get('paid_on') and cleaned['paid_on'] > timezone.localdate() + datetime.timedelta(days=31):
            self.add_error('paid_on', 'That date is too far ahead.')
        return cleaned

    def save(self, commit=True):
        from listings.utils import prepare_photo

        expense = super().save(commit=False)
        expense.owner = self.owner
        upload = self.cleaned_data.get('receipt_photo')
        if upload:
            content = prepare_photo(upload)  # resized, location data removed
            expense.receipt.save(content.name, content, save=False)
        if commit:
            expense.save()
        return expense
