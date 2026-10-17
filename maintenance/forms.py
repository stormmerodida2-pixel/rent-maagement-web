from django import forms
from django.utils.translation import gettext_lazy as _

from rentals.models import Property, Tenant

from .models import MaintenanceRequest


class TenantMaintenanceForm(forms.ModelForm):
    """Repair request submitted by a tenant from their portal, limited to units they rent."""

    class Meta:
        model = MaintenanceRequest
        fields = ['unit', 'title', 'description', 'priority']
        labels = {'unit': _('Unit'), 'title': _('What needs fixing?'), 'description': _('Details'),
                  'priority': _('How urgent is it?')}
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, units, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['unit'].queryset = units
        self.fields['unit'].empty_label = None
        if len(units) == 1:
            self.fields['unit'].widget = forms.HiddenInput()
            self.fields['unit'].initial = units[0].pk


class WhereMixin:
    """A single "Where" choice for a repair: one house, or "General" for something affecting the whole
    property (the gate, the water pump, the compound). Grouped by property."""

    def add_where(self, properties):
        properties = list(properties.prefetch_related('units'))
        self._places = {}
        choices = [('', 'Choose…')]
        for prop in properties:
            self._places[f'p{prop.pk}'] = (prop, None)
            group = [(f'p{prop.pk}', f'General: whole of {prop.name}')]
            for unit in sorted(prop.units.all(), key=lambda u: u.sort_key):
                self._places[f'u{unit.pk}'] = (prop, unit)
                group.append((f'u{unit.pk}', unit.label))
            choices.append((prop.name, group))
        self.fields['where'] = forms.ChoiceField(
            choices=choices, label='Where',
            help_text='Choose "General" for problems affecting most tenants: water, the gate, drainage, cleanliness.')
        self.order_fields(['where'])
        inst = self.instance
        if inst.pk:
            self.initial['where'] = f'u{inst.unit_id}' if inst.unit_id else f'p{inst.property_id}'
        elif len(properties) == 1 and not properties[0].units.exists():
            self.initial['where'] = f'p{properties[0].pk}'

    def add_notify(self):
        self.fields['notify_tenants'] = forms.BooleanField(
            required=False, label='Let the tenant know by SMS',
            help_text='For "General", every tenant in the property gets it. They hear when it\'s logged, when '
                      'work starts and when it\'s done, in their own language.')

    def clean_where(self):
        self.instance.property, self.instance.unit = self._places[self.cleaned_data['where']]
        return self.cleaned_data['where']


class MaintenanceForm(WhereMixin, forms.ModelForm):
    """Landlord's form for logging and updating repairs."""

    class Meta:
        model = MaintenanceRequest
        fields = ['reported_by', 'title', 'description', 'priority', 'status', 'assigned_to', 'cost']
        labels = {'title': 'Issue', 'cost': 'Cost (KES)', 'reported_by': 'Reported by (optional)'}
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_where(Property.objects.filter(owner=owner))
        self.add_notify()
        self.fields['reported_by'].queryset = Tenant.objects.filter(owner=owner)
        if not self.instance.pk:
            del self.fields['status']
