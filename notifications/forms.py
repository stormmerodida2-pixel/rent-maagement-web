from django import forms
from django.core.exceptions import ValidationError

from rentals.utils import normalize_phone

from .models import LandlordPreferences, SmsSettings, WhatsAppSettings


class PreferencesForm(forms.ModelForm):
    class Meta:
        model = LandlordPreferences
        fields = ['phone', 'report_email', 'report_sms', 'payment_summary', 'tax_reminders']
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


class SmsSettingsForm(forms.ModelForm):
    """The API key is never shown again; blank keeps the saved one (or the one on the server, the first time)."""

    class Meta:
        model = SmsSettings
        fields = ['live', 'username', 'api_key', 'sender_id']
        widgets = {'api_key': forms.PasswordInput(render_value=False)}

    def __init__(self, *args, **kwargs):
        from django.conf import settings

        super().__init__(*args, **kwargs)
        self.fallback_key = self.instance.api_key or settings.SMS.get('AT_API_KEY', '')
        if not self.instance.saved():  # first time: start from what's on the server
            self.initial.setdefault('username', '' if settings.SMS['AT_USERNAME'] == 'sandbox' else settings.SMS['AT_USERNAME'])
            self.initial.setdefault('sender_id', settings.SMS.get('AT_SENDER_ID', ''))
            self.initial.setdefault('live', settings.SMS['BACKEND'] == 'africastalking')
        if self.fallback_key:
            self.fields['api_key'].widget.attrs['placeholder'] = '•••••••• saved. Leave blank to keep.'
        self.fields['username'].required = self.fields['api_key'].required = False

    def clean_api_key(self):
        return self.cleaned_data['api_key'].strip() or self.fallback_key

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('live') and not (cleaned.get('username') and cleaned.get('api_key')):
            raise forms.ValidationError('To send real SMS, enter the username and API key.')
        return cleaned

