from django import forms

from .models import SiteSettings


class BusinessDetailsForm(forms.ModelForm):
    LEGAL_FIELDS = ['legal_name', 'address', 'privacy_email']
    CONTACT_FIELDS = ['support_phone', 'whatsapp']
    SOCIAL_FIELDS = SiteSettings.SOCIAL

    class Meta:
        model = SiteSettings
        fields = ['legal_name', 'address', 'privacy_email', 'support_phone', 'whatsapp', *SiteSettings.SOCIAL]
        widgets = {'address': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.SOCIAL_FIELDS:
            self.fields[name].widget.attrs['placeholder'] = f'https://{name}.com/…'
        for name in self.CONTACT_FIELDS:
            self.fields[name].widget.attrs.update(inputmode='tel', placeholder='0712 345 678')

    def groups(self):
        return [('Legal details', 'Shown on the terms of service and privacy policy.', self.LEGAL_FIELDS),
                ('Contact', 'Shown on the help page; WhatsApp also appears in the home page footer.', self.CONTACT_FIELDS),
                ('Social media', 'Round buttons in the home page footer; only the ones filled in are shown.',
                 self.SOCIAL_FIELDS)]

    def grouped(self):
        return [(title, text, [self[name] for name in names]) for title, text, names in self.groups()]

    def clean_whatsapp(self):
        value = self.cleaned_data['whatsapp'].strip()
        if value:
            from rentals.utils import normalize_phone

            try:
                normalize_phone(value)
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return value
