import datetime

from django import forms

from rentals.utils import normalize_phone

from .models import Application, Listing
from .utils import MAX_PHOTOS


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiplePhotoField(forms.FileField):
    widget = MultipleFileInput(attrs={'accept': 'image/jpeg,image/png,image/webp'})

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        return [super(MultiplePhotoField, self).clean(f, initial) for f in files]


def _clean_phone(value):
    value = (value or '').strip()
    if not value:
        return ''
    try:
        return normalize_phone(value)
    except ValueError as exc:
        raise forms.ValidationError(str(exc)) from exc


class ListingForm(forms.ModelForm):
    photos = MultiplePhotoField(required=False, label='Add photos',
                                help_text=f'Up to {MAX_PHOTOS} in total. Taken on a phone is fine.')

    class Meta:
        model = Listing
        fields = ['title', 'rent', 'deposit', 'available_from', 'description', 'contact_phone']
        widgets = {
            'available_from': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'description': forms.Textarea(attrs={'rows': 5}),
            'contact_phone': forms.TextInput(attrs={'inputmode': 'tel', 'placeholder': '0712 345 678'}),
        }

    def clean_contact_phone(self):
        return _clean_phone(self.cleaned_data['contact_phone'])


class ApplicationForm(forms.ModelForm):
    # Honeypot: hidden from people, filled in by bots.
    website = forms.CharField(required=False, widget=forms.TextInput(attrs={'autocomplete': 'off', 'tabindex': '-1'}))

    class Meta:
        model = Application
        fields = ['name', 'phone', 'email', 'move_in', 'occupants', 'message']
        widgets = {
            'phone': forms.TextInput(attrs={'inputmode': 'tel', 'placeholder': '0712 345 678', 'autocomplete': 'tel'}),
            'move_in': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'occupants': forms.NumberInput(attrs={'min': 1, 'max': 20}),
            'message': forms.Textarea(attrs={'rows': 3}),
        }

    def clean_phone(self):
        return _clean_phone(self.cleaned_data['phone']) or self.cleaned_data['phone']

    def clean_move_in(self):
        day = self.cleaned_data.get('move_in')
        if day and day < datetime.date.today():
            raise forms.ValidationError('Choose a date from today on.')
        return day
