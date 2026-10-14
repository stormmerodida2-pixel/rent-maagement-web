from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.urls import reverse_lazy
from django.utils.functional import lazy
from django.utils.html import format_html

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
