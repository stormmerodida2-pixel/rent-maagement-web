from django import forms
from django.core.exceptions import ValidationError

from rentals.utils import normalize_phone

from .models import LandlordPreferences


class PreferencesForm(forms.ModelForm):
    class Meta:
        model = LandlordPreferences
        fields = ['phone', 'report_email', 'report_sms']
        widgets = {'phone': forms.TextInput(attrs={'inputmode': 'tel', 'placeholder': '0712 345 678'})}

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        if not phone:
            return ''
        try:
            return normalize_phone(phone)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('report_sms') and not cleaned.get('phone'):
            self.add_error('phone', 'Add your phone number to get the SMS summary.')
        return cleaned
