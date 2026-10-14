from django import forms
from django.utils import timezone

from rentals.models import Lease

from .models import MpesaAccount, Payment

# Safaricom's published sandbox test credentials for Lipa Na M-Pesa Online.
SANDBOX_SHORTCODE = '174379'
SANDBOX_PASSKEY = 'bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919'
SECRET_FIELDS = ['consumer_key', 'consumer_secret', 'passkey']


class StkPushForm(forms.Form):
    amount = forms.DecimalField(min_value=1, max_digits=10, decimal_places=2)
    phone = forms.CharField(max_length=20, required=False, help_text="Defaults to the tenant's phone.")


class MpesaAccountForm(forms.ModelForm):
    """Secrets are never rendered back; leaving one blank keeps the saved value."""

    class Meta:
        model = MpesaAccount
        fields = ['environment', 'payment_type', 'shortcode', 'till_number', 'consumer_key', 'consumer_secret', 'passkey']
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
        if not self.instance.pk:
            self.initial.setdefault('shortcode', SANDBOX_SHORTCODE)
        self.fields['passkey'].help_text = (
            f'For sandbox testing with shortcode {SANDBOX_SHORTCODE}, use: {SANDBOX_PASSKEY}'
        )

    def clean(self):
        cleaned = super().clean()
        for name, saved in self.saved_secrets.items():
            if not cleaned.get(name):
                cleaned[name] = saved
        for name in ['shortcode', 'till_number', *SECRET_FIELDS]:
            if cleaned.get(name):
                cleaned[name] = cleaned[name].strip()
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
