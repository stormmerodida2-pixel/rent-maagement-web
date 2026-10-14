from django import forms
from django.utils import timezone

from rentals.models import Property, Unit

from .models import Announcement

DATETIME = forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M')


class AnnouncementForm(forms.ModelForm):
    class Meta:
        model = Announcement
        fields = ['title', 'body', 'audience', 'properties', 'units', 'send_sms', 'show_on_portal', 'pinned',
                  'expires_at', 'scheduled_for']
        labels = {'scheduled_for': 'Send later'}
        help_texts = {
            'scheduled_for': 'Leave blank to send as soon as you confirm.',
            'body': 'Up to 1,000 characters. SMS shows the first ~300; the rest is on their rent page.',
        }
        widgets = {
            'body': forms.Textarea(attrs={'rows': 5, 'maxlength': 1000}),
            'properties': forms.CheckboxSelectMultiple,
            'units': forms.CheckboxSelectMultiple,
            'expires_at': DATETIME,
            'scheduled_for': DATETIME,
        }

    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)
        self.fields['properties'].queryset = Property.objects.filter(owner=owner)
        self.fields['units'].queryset = Unit.objects.filter(property__owner=owner).select_related('property')
        self.fields['audience'].widget.attrs['id'] = 'id_audience'

    def clean(self):
        cleaned = super().clean()
        audience = cleaned.get('audience')
        if audience == Announcement.Audience.PROPERTIES and not cleaned.get('properties'):
            self.add_error('properties', 'Choose at least one property.')
        if audience == Announcement.Audience.UNITS and not cleaned.get('units'):
            self.add_error('units', 'Choose at least one unit.')
        if not cleaned.get('send_sms') and not cleaned.get('show_on_portal'):
            raise forms.ValidationError("Choose at least one way to reach tenants: SMS or their rent page.")
        when = cleaned.get('scheduled_for')
        if when and when <= timezone.now():
            self.add_error('scheduled_for', 'Choose a time in the future, or leave blank to send now.')
        expires = cleaned.get('expires_at')
        if expires and expires <= (when or timezone.now()):
            self.add_error('expires_at', 'This must be after the announcement goes out.')
        return cleaned

    def save(self, commit=True):
        announcement = super().save(commit=False)
        announcement.owner = self.owner
        if commit:
            announcement.save()
            self.save_m2m()
        return announcement
