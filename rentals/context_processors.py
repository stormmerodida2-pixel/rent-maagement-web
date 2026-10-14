from django.conf import settings


def branding(request):
    context = {'APP_NAME': settings.APP_NAME}
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated and request.resolver_match \
            and request.resolver_match.app_name in ('rentals', 'payments', 'maintenance', 'notifications', 'accounts', 'reports', 'audit'):
        # Lazily imported to keep this module importable before apps are ready.
        from maintenance.models import MaintenanceRequest
        from payments.models import C2BPayment, MpesaAccount

        context['nav_open_requests'] = MaintenanceRequest.objects.filter(
            unit__property__owner=user,
        ).exclude(
            status__in=[MaintenanceRequest.Status.RESOLVED, MaintenanceRequest.Status.CANCELLED],
        ).count()
        context['nav_unmatched_c2b'] = C2BPayment.objects.filter(
            account__owner=user, payment__isnull=True,
        ).count()
        account = MpesaAccount.objects.filter(owner=user).first()
        context['nav_mpesa_missing'] = account is None or not account.is_complete()
        from accounts.verification import is_verified
        from accounts.views import setup_steps

        context['email_unverified'] = not is_verified(user)
        context['nav_setup_incomplete'] = not all(step['done'] for step in setup_steps(user))
    return context
