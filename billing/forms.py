from django import forms

from .models import BillingSettings

SECRETS = ['mpesa_consumer_key', 'mpesa_consumer_secret', 'mpesa_passkey']


class BillingSettingsForm(forms.ModelForm):
    """Prices, trial and the Till. Daraja secrets are never shown again; blank keeps the saved value."""

    PLAN_FIELDS = ['price_per_unit', 'free_units', 'trial_days', 'grace_days', 'till', 'notify_email']
    PROMPT_FIELDS = ['mpesa_environment', 'mpesa_store_number', *SECRETS]

    class Meta:
        model = BillingSettings
        fields = ['price_per_unit', 'free_units', 'trial_days', 'grace_days', 'till', 'notify_email',
                  'mpesa_environment', 'mpesa_store_number', *SECRETS]
        widgets = {name: forms.PasswordInput(render_value=False) for name in SECRETS}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = {name: getattr(self.instance, name) for name in SECRETS}
        for name in SECRETS:
            if self.saved[name]:
                self.fields[name].widget.attrs['placeholder'] = '•••••••• saved. Leave blank to keep.'
        for name in ('trial_days', 'grace_days'):
            self.fields[name].widget.attrs['min'] = 0
        self.fields['till'].widget.attrs['inputmode'] = 'numeric'

    def plan_fields(self):
        return [self[name] for name in self.PLAN_FIELDS]

    def prompt_fields(self):
        return [self[name] for name in self.PROMPT_FIELDS]

    def clean(self):
        cleaned = super().clean()
        for name in SECRETS:
            cleaned[name] = (cleaned.get(name) or '').strip() or self.saved[name]
        if cleaned.get('price_per_unit') == 0:
            self.add_error('price_per_unit', 'Set a price, or switch billing off instead.')
        return cleaned
