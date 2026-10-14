from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm, UserCreationForm
from django.urls import reverse_lazy
from django.utils.functional import lazy
from django.utils.html import format_html

from rentals.utils import normalize_phone

TERMS_LABEL = lazy(lambda: format_html(
    'I agree to the <a href="{}" target="_blank">Terms of service</a> and the '
    '<a href="{}" target="_blank">Privacy policy</a>', reverse_lazy('terms'), reverse_lazy('privacy'),
), str)


class SignupForm(UserCreationForm):
    first_name = forms.CharField(max_length=150, label='Your name')
    email = forms.EmailField()
    accept_terms = forms.BooleanField(
        label=TERMS_LABEL(),
        error_messages={'required': 'Please read and accept the terms and privacy policy to continue.'},
    )

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ['first_name', 'email', 'username']
        help_texts = {'username': 'Letters, digits and @/./+/-/_ only. You will log in with this.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].widget.attrs.pop('autofocus', None)
        self.fields['first_name'].widget.attrs['autofocus'] = True
        self.fields['password1'].help_text = 'At least 8 characters. Avoid common words or only numbers.'
        self.fields['password2'].help_text = ''

    def clean_email(self):
        email = self.cleaned_data['email'].lower()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists. Try logging in.')
        return email


class ProfileForm(forms.ModelForm):
    """Name, email and phone. Changing the email needs the current password (it controls password resets)."""

    phone = forms.CharField(
        label='Your phone number', required=False, max_length=20,
        widget=forms.TextInput(attrs={'inputmode': 'tel', 'placeholder': '0712 345 678', 'autocomplete': 'tel'}),
        help_text='For SMS from us, such as the monthly summary.',
    )
    current_password = forms.CharField(
        label='Current password', required=False, strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}),
        help_text='Only needed if you change your email.',
    )

    class Meta:
        model = get_user_model()
        fields = ['first_name', 'email']
        labels = {'first_name': 'Your name', 'email': 'Email'}
        widgets = {'first_name': forms.TextInput(attrs={'autocomplete': 'name'}),
                   'email': forms.EmailInput(attrs={'autocomplete': 'email'})}

    def __init__(self, *args, prefs, **kwargs):
        self.prefs = prefs
        super().__init__(*args, **kwargs)
        self.original_email = self.instance.email
        self.fields['first_name'].required = True
        self.fields['email'].required = True
        self.fields['phone'].initial = prefs.phone

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if get_user_model().objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('Another account already uses this email.')
        return email

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        if not phone:
            return ''
        try:
            return normalize_phone(phone)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc

    @property
    def email_changed(self):
        return self.cleaned_data.get('email', '').lower() != (self.original_email or '').lower()

    def clean(self):
        cleaned = super().clean()
        if 'email' in cleaned and self.email_changed:
            if not self.instance.check_password(cleaned.get('current_password') or ''):
                self.add_error('current_password', 'Enter your current password to change your email.')
        if 'phone' in cleaned and not cleaned['phone'] and self.prefs.report_sms:
            self.add_error('phone', 'You get the monthly SMS summary. Turn it off under Notifications first, '
                                    'or keep a phone number.')
        return cleaned

    def save(self, commit=True):
        user = super().save(commit)
        self.prefs.phone = self.cleaned_data['phone']
        if commit:
            self.prefs.save(update_fields=['phone'])
        return user


class ChangePasswordForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['new_password1'].help_text = 'At least 8 characters. Avoid common words or only numbers.'
        self.fields['old_password'].label = 'Current password'
        self.fields['new_password2'].label = 'New password again'
        self.fields['new_password2'].help_text = ''
