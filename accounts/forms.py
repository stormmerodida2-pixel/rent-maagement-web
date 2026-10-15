import datetime

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import PasswordChangeForm, UserCreationForm
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.functional import lazy
from django.utils.html import format_html

from maintenance.models import MaintenanceRequest
from rentals.models import Property
from rentals.utils import normalize_phone

from .models import StaffMember

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


# --- Staff (caretakers) ----------------------------------------------------------------------------


def _clean_phone(value):
    value = value.strip()
    if not value:
        return ''
    try:
        return normalize_phone(value)
    except ValueError as exc:
        raise forms.ValidationError(str(exc)) from exc


class StaffForm(forms.Form):
    """Landlord adds or edits a staff member (and, optionally, sets their password)."""

    name = forms.CharField(max_length=150, label='Their name')
    username = forms.CharField(max_length=150, help_text='What they type to log in, e.g. "peter.caretaker".')
    phone = forms.CharField(max_length=20, required=False, label='Their phone',
                            widget=forms.TextInput(attrs={'inputmode': 'tel', 'placeholder': '0712 345 678'}))
    password = forms.CharField(
        strip=False, required=False, widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
        help_text='At least 8 characters. Give it to them in person; they can change it after logging in.',
    )
    properties = forms.ModelMultipleChoiceField(
        queryset=Property.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
        label='Properties they look after', help_text='Leave all unticked for every property.',
    )
    active = forms.BooleanField(required=False, initial=True, label='Can log in',
                                help_text='Untick when someone leaves. Their past work stays in the records.')

    def __init__(self, *args, owner, member=None, **kwargs):
        self.owner, self.member = owner, member
        super().__init__(*args, **kwargs)
        self.fields['properties'].queryset = Property.objects.filter(owner=owner)
        if member is None:
            self.fields['password'].required = True
            del self.fields['active']
        else:
            self.fields['password'].label = 'New password (optional)'
            self.fields['password'].help_text = 'Leave blank to keep their current password.'
            self.initial.update(name=member.name, username=member.user.username, phone=member.phone,
                                properties=member.property_ids(), active=member.user.is_active)

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        taken = get_user_model().objects.filter(username__iexact=username)
        if self.member:
            taken = taken.exclude(pk=self.member.user_id)
        if taken.exists():
            raise forms.ValidationError('That username is taken. Try adding their unit or a number.')
        get_user_model().username_validator(username)
        return username

    def clean_phone(self):
        return _clean_phone(self.cleaned_data['phone'])

    def clean_password(self):
        password = self.cleaned_data['password']
        if password:
            user = self.member.user if self.member else get_user_model()(username=self.cleaned_data.get('username', ''))
            password_validation.validate_password(password, user)
        return password

    def save(self):
        data = self.cleaned_data
        if self.member is None:
            user = get_user_model().objects.create_user(data['username'], password=data['password'],
                                                        first_name=data['name'])
            member = StaffMember.objects.create(owner=self.owner, user=user, phone=data['phone'])
        else:
            member, user = self.member, self.member.user
            user.username, user.first_name = data['username'], data['name']
            user.is_active = data['active']
            if data['password']:
                user.set_password(data['password'])
            user.save()
            member.phone = data['phone']
            member.save(update_fields=['phone'])
        member.properties.set(data['properties'])
        return member


class CashForm(forms.Form):
    amount = forms.DecimalField(label='Amount received (KES)', min_value=1, max_digits=10, decimal_places=2,
                                widget=forms.NumberInput(attrs={'inputmode': 'numeric', 'step': '1'}))
    paid_at = forms.DateTimeField(label='When', widget=forms.DateTimeInput(attrs={'type': 'datetime-local'},
                                                                         format='%Y-%m-%dT%H:%M'))
    reference = forms.CharField(label='Receipt book number', max_length=50, required=False)
    notes = forms.CharField(max_length=200, required=False, label='Notes')

    MAX_AGE = datetime.timedelta(days=7)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault('paid_at', timezone.localtime().replace(second=0, microsecond=0))

    def clean_paid_at(self):
        when, now = self.cleaned_data['paid_at'], timezone.now()
        if when > now + datetime.timedelta(minutes=5):
            raise forms.ValidationError("That's in the future.")
        if when < now - self.MAX_AGE:
            raise forms.ValidationError('Older cash payments must be recorded by the landlord.')
        return when


class RepairForm(forms.ModelForm):
    class Meta:
        model = MaintenanceRequest
        fields = ['unit', 'title', 'description', 'priority', 'status', 'assigned_to', 'cost']
        labels = {'title': 'What needs fixing?', 'assigned_to': 'Who is fixing it (optional)',
                  'cost': 'Cost (KES, optional)'}
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, member, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['unit'].queryset = member.units().select_related('property')
        if not self.instance.pk:
            del self.fields['status']
