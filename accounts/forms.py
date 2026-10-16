import datetime

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.forms import PasswordChangeForm, PasswordResetForm, UserCreationForm
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


def clean_login_contact(value, exclude_user=None):
    """('email', address) or ('phone', 2547…) from what someone typed; raises ValidationError if taken or invalid."""
    from django.core.validators import validate_email

    from .models import Profile

    value = (value or '').strip()
    if '@' in value:
        validate_email(value)
        email = value.lower()
        taken = get_user_model().objects.filter(email__iexact=email)
        if exclude_user is not None:
            taken = taken.exclude(pk=exclude_user.pk)
        if taken.exists():
            raise forms.ValidationError('An account with this email already exists. Try logging in.')
        return 'email', email
    try:
        phone = normalize_phone(value)
    except ValueError:
        raise forms.ValidationError('Enter an email address, or a Kenyan mobile number like 0712 345 678.') from None
    taken = Profile.objects.filter(phone=phone)
    if exclude_user is not None:
        taken = taken.exclude(user=exclude_user)
    if taken.exists():
        raise forms.ValidationError('An account with this phone number already exists. Try logging in.')
    return 'phone', phone


class SignupForm(UserCreationForm):
    """Name, an email address or phone number to log in with, and a password. No username."""

    first_name = forms.CharField(max_length=150, label='Your name',
                                 widget=forms.TextInput(attrs={'autocomplete': 'name', 'autofocus': True}))
    contact = forms.CharField(label='Email or phone number', max_length=254,
                              widget=forms.TextInput(attrs={'autocomplete': 'username', 'autocapitalize': 'none',
                                                            'placeholder': 'you@example.com or 0712 345 678'}),
                              help_text='You log in with this. A phone number gets a code by SMS to confirm it.')
    accept_terms = forms.BooleanField(
        label=TERMS_LABEL(),
        error_messages={'required': 'Please read and accept the terms and privacy policy to continue.'},
    )

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ['first_name']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(['first_name', 'contact', 'password1', 'password2', 'accept_terms'])
        self.fields['password1'].help_text = 'At least 8 characters. Avoid common words or only numbers.'
        self.fields['password2'].help_text = ''

    def clean_contact(self):
        self.contact_kind, value = clean_login_contact(self.cleaned_data['contact'])
        return value

    def save(self, commit=True):
        from .utils import new_username

        user = super().save(commit=False)
        user.username = new_username()
        user.email = self.cleaned_data['contact'] if self.contact_kind == 'email' else ''
        if commit:
            user.save()
        return user

    @property
    def phone(self):
        return self.cleaned_data['contact'] if self.contact_kind == 'phone' else None


class ProfileForm(forms.ModelForm):
    """Name, email and phone. Either can be used to log in, so at least one is needed, and changing either
    needs the current password (they control logging in and password resets)."""

    phone = forms.CharField(
        label='Your phone number', required=False, max_length=20,
        widget=forms.TextInput(attrs={'inputmode': 'tel', 'placeholder': '0712 345 678', 'autocomplete': 'tel'}),
        help_text='You can log in with it, and we send SMS here (such as the monthly summary).',
    )
    current_password = forms.CharField(
        label='Current password', required=False, strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}),
        help_text='Only needed if you change your email or phone number.',
    )

    class Meta:
        model = get_user_model()
        fields = ['first_name', 'email']
        labels = {'first_name': 'Your name', 'email': 'Email'}
        widgets = {'first_name': forms.TextInput(attrs={'autocomplete': 'name'}),
                   'email': forms.EmailInput(attrs={'autocomplete': 'email'})}

    def __init__(self, *args, prefs, **kwargs):
        from .models import profile_for

        self.prefs = prefs
        super().__init__(*args, **kwargs)
        self.profile = profile_for(self.instance)
        self.original_email = self.instance.email
        self.original_phone = self.profile.phone or ''
        self.fields['first_name'].required = True
        self.fields['email'].required = False
        self.fields['email'].help_text = 'You can log in with it, and we email bills and alerts here.'
        self.fields['phone'].initial = (f'0{self.original_phone[3:]}' if self.original_phone else prefs.phone)

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if email and email != (self.original_email or '').lower():
            clean_login_contact(email, exclude_user=self.instance)
        return email

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        if not phone:
            return ''
        try:
            phone = normalize_phone(phone)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        if phone != self.original_phone:
            clean_login_contact(phone, exclude_user=self.instance)
        return phone

    @property
    def email_changed(self):
        return self.cleaned_data.get('email', '').lower() != (self.original_email or '').lower()

    @property
    def phone_changed(self):
        return self.cleaned_data.get('phone', '') != self.original_phone

    def clean(self):
        cleaned = super().clean()
        if 'email' in cleaned and 'phone' in cleaned:
            if not cleaned['email'] and not cleaned['phone']:
                raise forms.ValidationError('Keep an email address or a phone number: you log in with them.')
            if (self.email_changed or self.phone_changed) and \
                    not self.instance.check_password(cleaned.get('current_password') or ''):
                self.add_error('current_password', 'Enter your current password to change your email or phone.')
        if 'phone' in cleaned and not cleaned['phone'] and self.prefs.report_sms:
            self.add_error('phone', 'You get the monthly SMS summary. Turn it off under Notifications first, '
                                    'or keep a phone number.')
        return cleaned

    def save(self, commit=True):
        user = super().save(commit)
        phone = self.cleaned_data['phone']
        self.prefs.phone = phone
        if self.phone_changed:
            self.profile.phone, self.profile.phone_verified_at = phone or None, None
        if self.email_changed:
            self.profile.email_verified_at = None
        if commit:
            self.prefs.save(update_fields=['phone'])
            self.profile.save(update_fields=['phone', 'phone_verified_at', 'email_verified_at'])
        return user


class ResetRequestForm(PasswordResetForm):
    """Django's reset form, but the account can be found by email or phone number."""

    email = forms.CharField(label='Email or phone number', max_length=254,
                            widget=forms.TextInput(attrs={'autocomplete': 'username', 'autocapitalize': 'none'}))

    @property
    def is_phone(self):
        return '@' not in self.cleaned_data.get('email', '')


class PhoneCodeForm(forms.Form):
    code = forms.CharField(label='6-digit code', max_length=10,
                           widget=forms.TextInput(attrs={'inputmode': 'numeric', 'autocomplete': 'one-time-code',
                                                         'autofocus': True}))


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
    phone = forms.CharField(max_length=20, label='Their phone number',
                            widget=forms.TextInput(attrs={'inputmode': 'tel', 'placeholder': '0712 345 678'}),
                            help_text='They log in with this number and the password you set.')
    password = forms.CharField(
        strip=False, required=False, widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
        help_text='At least 8 characters. Give it to them in person; they can change it after logging in.',
    )
    properties = forms.ModelMultipleChoiceField(
        queryset=Property.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
        label='Properties they look after', help_text='Leave all unticked for every property.',
    )
    can_read_meters = forms.BooleanField(required=False, initial=True, label='Can enter water meter readings',
                                         help_text='Water is billed from them, as when you enter readings.')
    can_record_spending = forms.BooleanField(required=False, initial=True,
                                             label='Can record spending from the cash they hold',
                                             help_text='With a receipt photo. You approve each one before it counts.')
    can_add_notes = forms.BooleanField(required=False, initial=True, label='Can add notes on tenants',
                                       help_text='e.g. "Promised to pay KES 10,000 by Friday". You see them on the tenant\'s page.')
    active = forms.BooleanField(required=False, initial=True, label='Can log in',
                                help_text='Untick when someone leaves. Their past work stays in the records.')

    PERMISSIONS = ['can_read_meters', 'can_record_spending', 'can_add_notes']

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
            self.initial.update(name=member.name, phone=member.phone, properties=member.property_ids(),
                                active=member.user.is_active,
                                **{name: getattr(member, name) for name in self.PERMISSIONS})

    def clean_phone(self):
        phone = _clean_phone(self.cleaned_data['phone'])
        if not phone:
            raise forms.ValidationError('Enter their phone number: they log in with it.')
        _, phone = clean_login_contact(phone, exclude_user=self.member.user if self.member else None)
        return phone

    def clean_password(self):
        password = self.cleaned_data['password']
        if password:
            user = self.member.user if self.member else get_user_model()(first_name=self.cleaned_data.get('name', ''))
            password_validation.validate_password(password, user)
        return password

    def save(self):
        data = self.cleaned_data
        from .models import Profile
        from .utils import new_username

        if self.member is None:
            user = get_user_model().objects.create_user(new_username(), password=data['password'],
                                                        first_name=data['name'])
            member = StaffMember.objects.create(owner=self.owner, user=user, phone=data['phone'])
        else:
            member, user = self.member, self.member.user
            user.first_name = data['name']
            user.is_active = data['active']
            if data['password']:
                user.set_password(data['password'])
            user.save()
            member.phone = data['phone']
            member.save(update_fields=['phone'])
        for name in self.PERMISSIONS:
            setattr(member, name, data[name])
        member.save(update_fields=self.PERMISSIONS)
        member.properties.set(data['properties'])
        # The landlord vouches for the number, so it counts as confirmed.
        Profile.objects.update_or_create(user=user, defaults={'phone': data['phone'], 'phone_verified_at': timezone.now()})
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


class StaffMeterForm(forms.Form):
    reading_date = forms.DateField(label='Date', widget=forms.DateInput(attrs={'type': 'date'}))
    value = forms.DecimalField(label='Meter reading', min_value=0, max_digits=12, decimal_places=2,
                               widget=forms.NumberInput(attrs={'inputmode': 'decimal', 'step': 'any'}))
    photo = forms.ImageField(label='Photo of the meter (optional)', required=False,
                             widget=forms.ClearableFileInput(attrs={'accept': 'image/*', 'capture': 'environment'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault('reading_date', timezone.localdate())


class StaffExpenseForm(forms.Form):
    amount = forms.DecimalField(label='Amount spent (KES)', min_value=1, max_digits=10, decimal_places=2,
                                widget=forms.NumberInput(attrs={'inputmode': 'numeric', 'step': '1'}))
    description = forms.CharField(label='What for', max_length=200,
                                  widget=forms.TextInput(attrs={'placeholder': 'e.g. New kitchen tap and plumber'}))
    unit = forms.ModelChoiceField(queryset=None, required=False, label='Which unit (optional)',
                                  empty_label='Not for one unit')
    spent_on = forms.DateField(label='Date', widget=forms.DateInput(attrs={'type': 'date'}))
    receipt = forms.ImageField(label='Photo of the receipt', required=False,
                               widget=forms.ClearableFileInput(attrs={'accept': 'image/*', 'capture': 'environment'}),
                               help_text='Strongly recommended: it helps the landlord approve it quickly.')

    def __init__(self, *args, member, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['unit'].queryset = member.units().select_related('property')
        self.initial.setdefault('spent_on', timezone.localdate())

    def clean_spent_on(self):
        day = self.cleaned_data['spent_on']
        if day > timezone.localdate():
            raise forms.ValidationError("That's in the future.")
        return day


class TenantNoteForm(forms.Form):
    text = forms.CharField(label='Note', max_length=300, widget=forms.Textarea(attrs={'rows': 2,
                           'placeholder': 'e.g. Says salary comes on the 25th; will pay then.'}))
    promised_amount = forms.DecimalField(label='Promised to pay (KES, optional)', required=False, min_value=1,
                                         max_digits=10, decimal_places=2,
                                         widget=forms.NumberInput(attrs={'inputmode': 'numeric', 'step': '1'}))
    promised_by = forms.DateField(label='By (optional)', required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('promised_amount') and not cleaned.get('promised_by'):
            self.add_error('promised_by', 'When did they promise to pay by?')
        return cleaned

