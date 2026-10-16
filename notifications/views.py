from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.generic import ListView

from rentals.utils import OwnedMixin, normalize_phone

from .forms import PreferencesForm
from .models import LandlordPreferences, SmsMessage
from .utils import previous_month, send_report_card


class SmsList(OwnedMixin, ListView):
    model = SmsMessage
    owner_path = 'lease__unit__property__owner'
    template_name = 'notifications/sms_list.html'
    paginate_by = 50

    def get_queryset(self):
        return super().get_queryset().select_related('lease__tenant')


@login_required
def preferences(request):
    prefs = LandlordPreferences.for_user(request.user)
    form = PreferencesForm(request.POST or None, instance=prefs)
    if request.method == 'POST':
        if request.POST.get('action') == 'preview':
            result = send_report_card(request.user, previous_month(), record=False)
            if result is None:
                messages.info(request, 'Nothing to report for last month yet: no rent billed or collected.')
            elif any(result):
                sent = ' and '.join(n for n, ok in zip(['email', 'SMS'], result, strict=True) if ok)
                messages.success(request, f"Sent last month's report card by {sent}.")
            else:
                messages.error(request, 'Turn on email (with a confirmed address) or SMS (with a phone number) first.')
            return redirect('notifications:preferences')
        if form.is_valid():
            form.save()
            messages.success(request, 'Saved.')
            return redirect('notifications:preferences')
    return render(request, 'notifications/preferences.html', {'form': form})


@login_required
def whatsapp_settings(request):
    """Settings → WhatsApp (administrators): Rentipay's WhatsApp Business number for payment summaries."""
    from audit import utils as audit

    from .forms import WhatsAppSettingsForm
    from .models import WhatsAppSettings
    from .utils import WhatsAppError, send_whatsapp_template, summary_text

    if not request.user.is_superuser:
        raise Http404
    conf = WhatsAppSettings.load()
    form = WhatsAppSettingsForm(request.POST or None, instance=conf)
    sample = [f'{timezone.localdate():%a %d %b}', 'KES 45,000', '3 M-Pesa, 1 cash by Peter',
              '4 tenants still owe KES 62,000.']
    if request.method == 'POST' and request.POST.get('action') == 'test':
        try:
            to = normalize_phone(request.POST.get('test_phone', ''))
            send_whatsapp_template(to, sample, conf)
        except ValueError as exc:
            messages.error(request, str(exc))
        except WhatsAppError as exc:
            messages.error(request, f'WhatsApp said: {exc}')
        else:
            messages.success(request, f'Sent a sample summary to {to}. Check WhatsApp on that phone.')
        return redirect('notifications:whatsapp')
    if request.method == 'POST' and form.is_valid():
        form.save()
        audit.record(owner=request.user, actor=request.user, action='whatsapp.settings_changed',
                     summary='Changed WhatsApp settings: ' + ', '.join(form.changed_data or ['nothing']) + '.')
        messages.success(request, 'WhatsApp settings saved. Send yourself a test to check them.')
        return redirect('notifications:whatsapp')
    return render(request, 'notifications/whatsapp_settings.html', {
        'form': form, 'ready': conf.ready(), 'sample_text': summary_text(sample),
        'my_phone': LandlordPreferences.for_user(request.user).phone,
    })

