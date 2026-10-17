"""Telling tenants about repairs: by SMS when the landlord or caretaker chooses to."""

from django.utils.translation import gettext as _

from rentals.models import Lease
from rentals.utils import tenant_language

from .models import MaintenanceRequest


def repair_text(repair, tenant, created):
    """The SMS for one tenant, in their language; None when there's nothing worth telling (cancelled)."""
    status = MaintenanceRequest.Status
    with tenant_language(tenant):
        args = {'name': tenant.full_name.split()[0] if tenant.full_name else '', 'title': repair.title}
        if repair.status == status.RESOLVED:
            return _('Hi %(name)s, this repair is done: %(title)s. Thank you for your patience.') % args
        if repair.status == status.IN_PROGRESS:
            return _('Hi %(name)s, work has started on this repair: %(title)s.') % args
        if repair.status == status.OPEN:
            if created:
                return _("Hi %(name)s, your landlord knows about this and will fix it: %(title)s. We'll let you "
                         'know when it is done.') % args
            return _('Hi %(name)s, update on this repair: %(title)s. It will be fixed soon.') % args
    return None


def notify_tenants(repair, created):
    """SMS the tenants a repair affects: the house's tenant, or everyone in the property for a General repair.
    Returns how many messages were sent (or logged)."""
    from notifications.models import SmsMessage
    from notifications.utils import send_personal_sms

    leases = Lease.objects.filter(is_active=True).select_related('tenant')
    leases = leases.filter(unit=repair.unit) if repair.unit_id else leases.filter(unit__property=repair.property)
    items = []
    for lease in leases:
        if lease.tenant.phone and (text := repair_text(repair, lease.tenant, created)):
            items.append((lease, lease.tenant.phone, text))
    if not items:
        return 0
    sent = send_personal_sms(items)
    return sum(m.status != SmsMessage.Status.FAILED for m in sent)
