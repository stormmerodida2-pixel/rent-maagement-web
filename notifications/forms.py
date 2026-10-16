from django import forms
from django.core.exceptions import ValidationError

from rentals.utils import normalize_phone

from .models import LandlordPreferences, WhatsAppSettings


class PreferencesForm(forms.ModelForm):
    class Meta:
        model = LandlordPreferences
        fields = ['phone', 'report_email', 'report_sms', 'payment_summary']
        widgets = {'phone': forms.TextInput(attrs={'inputmode': 'tel', 'placeholder': '0712 345 678'})}

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        if not phone:
            return ''
        try:
            return normalize_phone(phone)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def _login_phone(self):
        from accounts.models import Profile

        return Profile.objects.filter(user_id=self.instance.user_id).exclude(phone=None).exists()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('report_sms') and not cleaned.get('phone'):
            self.add_error('phone', 'Add your phone number to get the SMS summary.')
        if cleaned.get('payment_summary') and not cleaned.get('phone') and not self._login_phone():
            self.add_error('phone', 'Add your WhatsApp number to get the payment summaries.')
        return cleaned


class WhatsAppSettingsForm(forms.ModelForm):
    """The access token is never shown again; leaving it blank keeps the saved one."""

    class Meta:
        model = WhatsAppSettings
        fields = ['phone_number_id', 'access_token', 'template_name', 'template_language']
        widgets = {'access_token': forms.PasswordInput(render_value=False)}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved_token = self.instance.access_token
        if self.saved_token:
            self.fields['access_token'].widget.attrs['placeholder'] = '•••••••• saved. Leave blank to keep.'

    def clean_access_token(self):
        return self.cleaned_data['access_token'].strip() or self.saved_token
