from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.generic import ListView

from rentals.utils import OwnedMixin

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
