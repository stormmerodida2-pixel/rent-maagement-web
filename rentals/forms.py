import datetime

from django import forms
from django.core.exceptions import ValidationError

from .models import Lease, Property, RentCharge, Tenant, Unit

DATE = forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d')


class OwnedForm(forms.ModelForm):
    """ModelForm that knows the signed-in landlord, to scope choices and set ownership."""

    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)


class PropertyForm(OwnedForm):
    # Optional quick-start: create a run of numbered units together with the property.
    unit_prefix = forms.CharField(
        required=False, max_length=10, label='Unit label prefix', help_text='e.g. "A" gives A1, A2, A3…',
    )
    unit_count = forms.IntegerField(required=False, min_value=1, max_value=200, label='Number of units')
    unit_rent = forms.DecimalField(required=False, min_value=0, max_digits=10, decimal_places=2,
                                   label='Monthly rent per unit (KES)')

    class Meta:
        model = Property
        fields = ['name', 'address', 'water_rate']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            for name in ['unit_prefix', 'unit_count', 'unit_rent']:
                del self.fields[name]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('unit_count') and cleaned.get('unit_rent') is None:
            self.add_error('unit_rent', 'Enter the rent for these units.')
        return cleaned

    def save(self, commit=True):
        prop = super().save(commit=False)
        prop.owner = self.owner
        if commit:
            prop.save()
            count = self.cleaned_data.get('unit_count')
            if count:
                prefix = self.cleaned_data.get('unit_prefix') or ''
                Unit.objects.bulk_create(
                    Unit(property=prop, label=f'{prefix}{i}', default_rent=self.cleaned_data['unit_rent'])
                    for i in range(1, count + 1)
                )
        return prop


class UnitForm(forms.ModelForm):
    class Meta:
        model = Unit
        fields = ['label', 'unit_type', 'bedrooms', 'default_rent']
        labels = {'default_rent': 'Monthly rent (KES)'}
        widgets = {'unit_type': forms.TextInput(attrs={'list': 'unit-types'})}

    def __init__(self, *args, property, **kwargs):
        self.property = property
        super().__init__(*args, **kwargs)
        self.instance.property = property

    def clean_label(self):
        label = self.cleaned_data['label'].strip()
        clash = Unit.objects.filter(property=self.property, label__iexact=label).exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError(f'{self.property} already has a unit called {label}.')
        return label


class TenantForm(OwnedForm):
    class Meta:
        model = Tenant
        fields = ['full_name', 'phone', 'language', 'email', 'id_number']
        labels = {'phone': 'M-Pesa phone number', 'language': 'Language for SMS and their rent page'}
        help_texts = {'id_number': 'Stored encrypted.'}
        widgets = {'phone': forms.TextInput(attrs={'inputmode': 'tel', 'placeholder': '0712 345 678'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.owner = self.owner
        self.fields['language'].required = False  # English unless chosen

    def clean_language(self):
        return self.cleaned_data.get('language') or 'en'


class LeaseForm(OwnedForm):
    bill_now = forms.BooleanField(
        required=False, initial=True, label="Bill this month's rent now",
        help_text='Otherwise it is billed by the next monthly run.',
    )
    bill_deposit = forms.BooleanField(
        required=False, initial=True, label='Bill the deposit now',
        help_text='Adds the deposit to what the tenant owes, so you can track when it is paid.',
    )

    class Meta:
        model = Lease
        fields = ['unit', 'tenant', 'start_date', 'monthly_rent', 'service_charge', 'deposit',
                  'due_day', 'late_fee', 'late_fee_grace_days']
        labels = {
            'monthly_rent': 'Monthly rent (KES)', 'deposit': 'Deposit (KES)',
            'service_charge': 'Service charge (KES / month)', 'late_fee': 'Late fee (KES)',
            'late_fee_grace_days': 'Grace period (days)',
        }
        widgets = {'start_date': DATE}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        units = Unit.objects.filter(property__owner=self.owner).select_related('property')
        occupied = Lease.objects.filter(is_active=True).exclude(pk=self.instance.pk).values('unit')
        self.fields['unit'].queryset = units.exclude(pk__in=occupied)
        self.fields['unit'].empty_label = 'Choose a vacant unit'
        self.fields['tenant'].queryset = Tenant.objects.filter(owner=self.owner)
        self.fields['tenant'].empty_label = 'Choose a tenant'
        self.fields['monthly_rent'].required = False
        self.fields['monthly_rent'].help_text = "Leave blank to use the unit's rent."
        self.fields['due_day'].label = 'Rent day'
        if not self.instance.pk:
            self.fields['due_day'].required = False
            self.fields['due_day'].initial = None
            self.fields['due_day'].help_text = ('Leave blank to use the move-in day: moved in on the 12th, rent is due '
                                                'every 12th and covers a month from then.')
        for name in self.OPTIONAL_WITH_DEFAULT:
            self.fields[name].required = False
        if self.instance.pk:
            del self.fields['bill_now']
            del self.fields['bill_deposit']
        else:
            self.initial.setdefault('start_date', datetime.date.today())

    # Blank means "use the model default" (no service charge, no late fee, 3-day grace).
    OPTIONAL_WITH_DEFAULT = ['deposit', 'service_charge', 'late_fee', 'late_fee_grace_days']

    def clean(self):
        cleaned = super().clean()
        for name in self.OPTIONAL_WITH_DEFAULT:
            if cleaned.get(name) is None and name not in self.errors:
                cleaned[name] = Lease._meta.get_field(name).get_default()
        unit = cleaned.get('unit')
        if unit and cleaned.get('monthly_rent') is None:
            cleaned['monthly_rent'] = unit.default_rent
            self.instance.monthly_rent = unit.default_rent
        if cleaned.get('due_day') is None and 'due_day' not in self.errors and cleaned.get('start_date'):
            # Rent day = move-in day: moved in on the 12th, each month runs 12th to 11th.
            cleaned['due_day'] = cleaned['start_date'].day
            self.instance.due_day = cleaned['due_day']
        return cleaned

    def save(self, commit=True):
        lease = super().save(commit)
        if commit and self.cleaned_data.get('bill_deposit'):
            lease.bill_deposit()
        if commit and self.cleaned_data.get('bill_now') and lease.start_date <= datetime.date.today():
            lease.ensure_charge_for(datetime.date.today())
        return lease


class NoticeForm(forms.Form):
    """A tenant (or the landlord) gives notice: when they will move out."""

    move_out = forms.DateField(widget=DATE, label='Move-out date',
                               help_text="Defaults to the end of a rent month. No new month is billed after it.")
    given_on = forms.DateField(widget=DATE, label='Notice given on', initial=datetime.date.today)
    note = forms.CharField(max_length=200, required=False, label='Note',
                           help_text='e.g. "Moving to Nakuru for work", or who gave notice.')

    def __init__(self, *args, lease, **kwargs):
        self.lease = lease
        super().__init__(*args, **kwargs)
        if lease.end_date:
            self.initial.update(move_out=lease.end_date, given_on=lease.notice_given_on, note=lease.notice_note)
        else:
            self.initial.setdefault('move_out', lease.default_move_out())

    def clean_move_out(self):
        move_out = self.cleaned_data['move_out']
        if move_out < datetime.date.today():
            raise ValidationError('That date has passed. If they have already left, use "Move out" instead.')
        if move_out < self.lease.start_date:
            raise ValidationError('The move-out date is before they moved in.')
        return move_out

    def clean_given_on(self):
        given_on = self.cleaned_data['given_on']
        if given_on > datetime.date.today():
            raise ValidationError("Notice can't be given in the future.")
        return given_on


class EndLeaseForm(forms.Form):
    end_date = forms.DateField(initial=datetime.date.today, widget=DATE, label='Move-out date')
    deductions = forms.DecimalField(
        required=False, min_value=0, max_digits=10, decimal_places=2, label='Deductions (KES)',
        help_text='Damage, cleaning or other costs taken from the deposit.',
    )
    reason = forms.CharField(required=False, max_length=150, label='Reason for deductions')
    apply_deposit = forms.BooleanField(
        required=False, initial=True, label='Apply the deposit to what they owe',
    )
    refund_paid = forms.BooleanField(
        required=False, label="I've already paid back any refund",
        help_text='Records the refund so their account closes at zero.',
    )

    def __init__(self, *args, lease, **kwargs):
        self.lease = lease
        super().__init__(*args, **kwargs)

    def clean_end_date(self):
        end = self.cleaned_data['end_date']
        if end < self.lease.start_date:
            raise ValidationError('They can\'t move out before they moved in.')
        return end

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('deductions') and not cleaned.get('reason'):
            self.add_error('reason', 'Say what the deductions are for. The tenant will see this.')
        return cleaned


class WaterReadingsForm(forms.Form):
    """One meter-reading field per unit, plus the reading date."""

    reading_date = forms.DateField(initial=datetime.date.today, widget=DATE)

    def __init__(self, *args, units, **kwargs):
        super().__init__(*args, **kwargs)
        self.units = units
        for unit in units:
            self.fields[f'unit_{unit.pk}'] = forms.DecimalField(
                required=False, min_value=0, max_digits=12, decimal_places=2, label=unit.label,
            )

    def readings(self):
        """(unit, value) pairs for the units that were filled in."""
        return [(u, self.cleaned_data[f'unit_{u.pk}']) for u in self.units
                if self.cleaned_data.get(f'unit_{u.pk}') is not None]


class ChargeForm(forms.ModelForm):
    """One-off charge or credit added by the landlord."""

    KINDS = [RentCharge.Kind.OTHER, RentCharge.Kind.WATER, RentCharge.Kind.DEDUCTION, RentCharge.Kind.REFUND]

    class Meta:
        model = RentCharge
        fields = ['kind', 'description', 'amount', 'due_date']
        labels = {'kind': 'Type', 'amount': 'Amount (KES)', 'due_date': 'Date'}
        widgets = {'due_date': DATE}

    def __init__(self, *args, lease, **kwargs):
        self.lease = lease
        super().__init__(*args, **kwargs)
        self.fields['kind'].choices = [(k.value, k.label) for k in self.KINDS]
        self.fields['description'].required = True
        self.fields['description'].help_text = 'Shown on the tenant\'s statement, e.g. "Replaced broken window".'
        self.initial.setdefault('due_date', datetime.date.today())

    def clean_amount(self):
        if self.cleaned_data['amount'] == 0:
            raise ValidationError('Enter an amount other than zero.')
        return self.cleaned_data['amount']

    def save(self, commit=True):
        charge = super().save(commit=False)
        charge.lease = self.lease
        charge.period = charge.due_date.replace(day=1)
        if commit:
            charge.save()
        return charge


class RentChangeForm(forms.Form):
    """Change one tenant's rent from a future rent day."""

    new_rent = forms.DecimalField(label='New monthly rent (KES)', min_value=1, max_digits=10, decimal_places=2)
    effective_from = forms.DateField(
        widget=DATE, label='Starting from',
        help_text='The new rent applies from the first rent day on or after this date. Give tenants at least a '
                  "month's notice.",
    )
    notify = forms.BooleanField(required=False, initial=True, label='Tell the tenant by SMS now')

    def __init__(self, *args, lease, **kwargs):
        self.lease = lease
        super().__init__(*args, **kwargs)
        self.initial.setdefault('effective_from', lease.first_rent_day_from(datetime.date.today()
                                                                            + datetime.timedelta(days=30)))

    def clean_effective_from(self):
        day = self.cleaned_data['effective_from']
        if day <= datetime.date.today():
            raise ValidationError('Choose a date in the future.')
        if self.lease.end_date and self.lease.first_rent_day_from(day) > self.lease.end_date:
            raise ValidationError('They move out before then.')
        return day

    def clean_new_rent(self):
        rent = self.cleaned_data['new_rent']
        if rent == self.lease.monthly_rent:
            raise ValidationError("That's their current rent.")
        return rent


class BulkRentChangeForm(forms.Form):
    """Change rent for several tenants in a property at once."""

    MODES = [('percent', 'Raise by a percentage'), ('amount', 'Raise by an amount (KES)'),
             ('set', 'Set everyone to the same rent (KES)')]
    mode = forms.ChoiceField(choices=MODES, initial='percent', label='How')
    value = forms.DecimalField(max_digits=10, decimal_places=2, label='By how much',
                               help_text='e.g. 10 for 10%, or 1000 for KES 1,000. Use a minus to lower rent.')
    effective_from = forms.DateField(widget=DATE, label='Starting from',
                                     help_text='Each tenant changes on their first rent day on or after this date.')
    leases = forms.ModelMultipleChoiceField(queryset=Lease.objects.none(), widget=forms.CheckboxSelectMultiple,
                                            label='Tenants')
    notify = forms.BooleanField(required=False, initial=True, label='Tell each tenant by SMS')

    def __init__(self, *args, property, **kwargs):
        super().__init__(*args, **kwargs)
        leases = (Lease.objects.filter(unit__property=property, is_active=True, end_date__isnull=True)
                  .select_related('tenant', 'unit').order_by('unit__label'))
        self.fields['leases'].queryset = leases
        self.fields['leases'].label_from_instance = lambda l: f'{l.unit.label} · {l.tenant} · KES {l.monthly_rent:,.0f}'
        self.initial.setdefault('leases', list(leases))
        today = datetime.date.today()
        self.initial.setdefault('effective_from', (today + datetime.timedelta(days=62)).replace(day=1))

    def clean_effective_from(self):
        day = self.cleaned_data['effective_from']
        if day <= datetime.date.today():
            raise ValidationError('Choose a date in the future.')
        return day

    def clean(self):
        cleaned = super().clean()
        mode, value = cleaned.get('mode'), cleaned.get('value')
        if mode == 'set' and value is not None and value < 1:
            self.add_error('value', 'Enter the new rent.')
        if mode in ('percent', 'amount') and value == 0:
            self.add_error('value', 'Enter how much to change it by.')
        return cleaned
