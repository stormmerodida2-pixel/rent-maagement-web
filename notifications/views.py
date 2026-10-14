from django.views.generic import ListView

from rentals.mixins import OwnedMixin

from .models import SmsMessage


class SmsList(OwnedMixin, ListView):
    model = SmsMessage
    owner_path = 'lease__unit__property__owner'
    template_name = 'notifications/sms_list.html'
    paginate_by = 50

    def get_queryset(self):
        return super().get_queryset().select_related('lease__tenant')
