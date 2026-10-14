from django import forms

from rentals.models import Tenant, Unit

from .models import MaintenanceRequest


class TenantMaintenanceForm(forms.ModelForm):
    """Repair request submitted by a tenant from their portal, limited to units they rent."""

    class Meta:
        model = MaintenanceRequest
        fields = ['unit', 'title', 'description', 'priority']
        labels = {'title': 'What needs fixing?', 'description': 'Details'}
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, units, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['unit'].queryset = units
        self.fields['unit'].empty_label = None
        if len(units) == 1:
            self.fields['unit'].widget = forms.HiddenInput()
            self.fields['unit'].initial = units[0].pk


class MaintenanceForm(forms.ModelForm):
    """Landlord's form for logging and updating repairs."""

    class Meta:
        model = MaintenanceRequest
        fields = ['unit', 'reported_by', 'title', 'description', 'priority', 'status', 'assigned_to', 'cost']
        labels = {'title': 'Issue', 'cost': 'Cost (KES)', 'reported_by': 'Reported by (optional)'}
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['unit'].queryset = Unit.objects.filter(property__owner=owner).select_related('property')
        self.fields['reported_by'].queryset = Tenant.objects.filter(owner=owner)
        if not self.instance.pk:
            del self.fields['status']
