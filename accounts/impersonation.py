"""Support: a superadmin sees the app exactly as one landlord does, to help them.

Started from the Landlords page (administrators only) with one click. Safeguards: superadmins only, never another
superadmin, caretaker or read-only viewer; the landlord's password, two-step and the staff area stay off limits;
ends by itself after LIMIT; a banner (for the superadmin) shows on every page with a Stop button. The landlord isn't
told: each session goes in a private SupportSession log that only superadmins see, and changes made show in the
landlord's Activity as "Rentipay support".
"""

import time

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from django.utils import timezone

from .models import SupportSession

SESSION_KEY = 'impersonate'
LIMIT = 60 * 60  # seconds
# Never while impersonating, even with changes allowed: the landlord's own login secrets and the staff area.
BLOCKED_PATHS = ('/settings/password/', '/settings/two-step/', '/staff/', '/admin/', '/verify-phone/')
STOP_PATH = '/support/impersonate/stop/'
# Always as the superadmin, even mid-impersonation: the Landlords list, starting (another) and stopping.
ADMIN_PATHS = ('/support/landlords/', '/support/impersonate/')


def can_impersonate(user):
    return user.is_authenticated and user.is_active and user.is_superuser


def eligible_landlords():
    """Landlords only: not superadmins, caretakers or people with read-only access."""
    return (get_user_model().objects.filter(is_active=True, is_superuser=False, staff_membership__isnull=True,
                                            viewer_access__isnull=True))


class ImpersonationMiddleware:
    """After authentication: while a superadmin impersonates, request.user is the landlord, request.impersonator is
    the superadmin and request.impersonation the session details."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.impersonator = None
        request.impersonation = None
        data = request.session.get(SESSION_KEY) if hasattr(request, 'session') else None
        if not data:
            return self.get_response(request)
        real = request.user
        target = eligible_landlords().filter(pk=data.get('id')).first()
        expired = time.time() - data.get('started', 0) > LIMIT
        if not can_impersonate(real) or data.get('by') != real.pk or target is None or expired:
            request.session.pop(SESSION_KEY, None)
            _close(data)
            if expired and can_impersonate(real):
                messages.info(request, 'Your support session ended after an hour.')
            return self.get_response(request)
        if request.path.startswith(ADMIN_PATHS):
            request.impersonation = data  # the banner still shows who you're impersonating
            return self.get_response(request)  # runs as the superadmin
        if request.path.startswith(BLOCKED_PATHS):
            messages.info(request, "That page isn't available while viewing a landlord's account.")
            return redirect('rentals:dashboard')

        from audit.utils import acting_user

        request.impersonator, request.impersonation, request.user = real, data, target
        token = acting_user.set(real)  # anything changed is logged under the superadmin's name
        try:
            return self.get_response(request)
        finally:
            acting_user.reset(token)


@login_required
def landlords(request):
    """Administrators: every landlord, with how far they've got, and an Impersonate button each."""
    from django.db.models import Count, Max

    if not can_impersonate(request.user):
        raise PermissionDenied
    q, show = request.GET.get('q', '').strip(), request.GET.get('show', 'all')
    qs = eligible_landlords().select_related('profile').annotate(
        n_properties=Count('properties', distinct=True),
        n_units=Count('properties__units', distinct=True),
        n_let=Count('properties__units__leases', filter=Q(properties__units__leases__is_active=True), distinct=True),
        last_activity=Max('audit_entries__created_at'))
    if q:
        qs = qs.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q)
                       | Q(profile__phone__icontains=q) | Q(properties__name__icontains=q)).distinct()
    counts = {'all': qs.count(), 'using': qs.filter(n_let__gt=0).count(), 'new': qs.filter(n_properties=0).count()}
    if show == 'using':
        qs = qs.filter(n_let__gt=0)
    elif show == 'new':
        qs = qs.filter(n_properties=0)
    from django.core.paginator import Paginator

    page = Paginator(qs.order_by('-date_joined'), 50).get_page(request.GET.get('page'))
    _add_billing(page.object_list)
    data = request.session.get(SESSION_KEY) or {}
    return render(request, 'accounts/landlords.html', {
        'page_obj': page, 'landlords': page.object_list, 'q': q, 'show': show, 'counts': counts,
        'impersonating': get_user_model().objects.filter(pk=data.get('id')).first() if data else None,
    })


def _add_billing(users):
    """Each landlord's billing status, if billing has started for them."""
    from billing.models import BillingAccount
    from billing.utils import status

    accounts = {a.owner_id: a for a in BillingAccount.objects.filter(owner__in=list(users))}
    for user in users:
        account = accounts.get(user.pk)
        user.billing_status = status(account) if account else ''


@login_required
@require_POST
def start(request, pk):
    if not can_impersonate(request.user):
        raise PermissionDenied
    target = get_object_or_404(eligible_landlords(), pk=pk)
    _close(request.session.get(SESSION_KEY))  # switching straight from another landlord
    log = SupportSession.objects.create(superadmin=request.user, landlord=target, allow_changes=True,
                                        reason=' '.join(request.POST.get('reason', '').split())[:150])
    request.session[SESSION_KEY] = {'id': target.pk, 'by': request.user.pk, 'started': time.time(), 'log': log.pk}
    return redirect('rentals:dashboard')


@require_POST
def stop(request):
    data = request.session.pop(SESSION_KEY, None)
    if data:
        _close(data)
        messages.success(request, 'Back to your own account.')
    return redirect('admin_landlords')


def _close(data):
    """Mark the private log entry for this session as ended (stopped, timed out or no longer allowed)."""
    if data and data.get('log'):
        SupportSession.objects.filter(pk=data['log'], ended_at__isnull=True).update(ended_at=timezone.now())
