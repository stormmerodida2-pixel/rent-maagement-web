"""Support: a superadmin sees the app exactly as one landlord does, to help them.

Safeguards: superadmins only, never another superadmin; a reason is required; view only unless "allow changes" is
chosen; ends by itself after LIMIT; a banner shows on every page with a Stop button; and the landlord's own
Activity log records who looked, when and why (changes made are logged under the superadmin's name).
"""

import time

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from audit import utils as audit

SESSION_KEY = 'impersonate'
LIMIT = 60 * 60  # seconds
# Never while impersonating, even with changes allowed: the landlord's own login secrets and the staff area.
BLOCKED_PATHS = ('/settings/password/', '/settings/two-step/', '/staff/', '/admin/', '/verify-phone/')
STOP_PATH = '/support/impersonate/stop/'


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
            if expired and can_impersonate(real):
                messages.info(request, 'Your support session ended after an hour.')
            return self.get_response(request)
        if request.path == STOP_PATH:
            return self.get_response(request)  # stop runs as the superadmin
        if request.path.startswith(BLOCKED_PATHS):
            messages.info(request, "That page isn't available while viewing a landlord's account.")
            return redirect('rentals:dashboard')
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not data.get('changes'):
            messages.error(request, 'This support session is view only, so nothing was changed.')
            back = request.META.get('HTTP_REFERER', '')
            safe = url_has_allowed_host_and_scheme(back, {request.get_host()}, request.is_secure())
            return redirect(back if safe and back else 'rentals:dashboard')

        from audit.utils import acting_user

        request.impersonator, request.impersonation, request.user = real, data, target
        token = acting_user.set(real)  # anything changed is logged under the superadmin's name
        try:
            return self.get_response(request)
        finally:
            acting_user.reset(token)


@login_required
def choose(request):
    """Superadmins: find a landlord to view as."""
    if not can_impersonate(request.user):
        raise PermissionDenied
    q = request.GET.get('q', '').strip()
    landlords = eligible_landlords().filter(properties__isnull=False).distinct()
    if q:
        landlords = landlords.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q)
                                     | Q(profile__phone__icontains=q) | Q(properties__name__icontains=q)).distinct()
    return render(request, 'accounts/impersonate.html', {
        'landlords': landlords.order_by('first_name', 'last_name')[:50], 'q': q, 'limit_minutes': LIMIT // 60,
    })


@login_required
@require_POST
def start(request, pk):
    if not can_impersonate(request.user):
        raise PermissionDenied
    target = get_object_or_404(eligible_landlords(), pk=pk)
    reason = ' '.join(request.POST.get('reason', '').split())[:150]
    if not reason:
        messages.error(request, 'Say why you need to see their account: it goes in their Activity log.')
        return redirect('impersonate')
    changes = request.POST.get('mode') == 'changes'
    request.session[SESSION_KEY] = {'id': target.pk, 'by': request.user.pk, 'started': time.time(),
                                    'changes': changes, 'reason': reason}
    who = request.user.get_full_name() or request.user.get_username()
    audit.record(owner=target, actor=request.user, action='support.started',
                 summary=f'{who} from support opened your account ({"could make changes" if changes else "view only"}). '
                         f'Reason: {reason}')
    return redirect('rentals:dashboard')


@require_POST
def stop(request):
    data = request.session.pop(SESSION_KEY, None)
    if data and request.user.is_authenticated:
        target = get_user_model().objects.filter(pk=data.get('id')).first()
        if target is not None:
            minutes = max(1, round((time.time() - data.get('started', time.time())) / 60))
            who = request.user.get_full_name() or request.user.get_username()
            audit.record(owner=target, actor=request.user, action='support.ended',
                         summary=f'{who} from support closed your account after {minutes} minute'
                                 f'{"s" if minutes != 1 else ""}.')
        messages.success(request, 'Back to your own account.')
    return redirect('impersonate')
