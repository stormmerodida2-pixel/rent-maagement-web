from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Complaint, SupportMessage


class ComplaintForm(forms.ModelForm):
    message = forms.CharField(label=_('Message'), widget=forms.Textarea(attrs={'rows': 4}), max_length=2000,
                              help_text=_('Your landlord will reply here. You will get an SMS when they do.'))

    class Meta:
        model = Complaint
        fields = ['category', 'subject']
        labels = {'category': _('What is it about?'), 'subject': _('Subject')}


class ReplyForm(forms.Form):
    body = forms.CharField(label=_('Your reply'), widget=forms.Textarea(attrs={'rows': 3}), max_length=2000)


class LandlordReplyForm(forms.Form):
    body = forms.CharField(label='Reply to tenant', required=False, widget=forms.Textarea(attrs={'rows': 3}),
                           max_length=2000, help_text='The tenant gets an SMS with a link to read it.')
    status = forms.ChoiceField(choices=Complaint.Status.choices)

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('body') and cleaned.get('status') == self.initial.get('status'):
            raise forms.ValidationError('Write a reply or change the status.')
        return cleaned


class ContactForm(forms.ModelForm):
    # Honeypot: hidden from people, filled in by bots.
    website = forms.CharField(required=False, widget=forms.TextInput(attrs={'autocomplete': 'off', 'tabindex': '-1'}))

    class Meta:
        model = SupportMessage
        fields = ['name', 'email', 'topic', 'message']
        labels = {'topic': 'What is it about?'}
        widgets = {'message': forms.Textarea(attrs={'rows': 5})}
