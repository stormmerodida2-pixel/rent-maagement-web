from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import connection
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from .utils import backup_status


@never_cache
def healthz(request):
    """For uptime monitors and load balancers. 200 when the database and cache respond."""
    checks = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        checks['database'] = 'ok'
    except Exception:
        checks['database'] = 'error'
    try:
        cache.set('ops:healthz', 1, 10)
        checks['cache'] = 'ok' if cache.get('ops:healthz') == 1 else 'error'
    except Exception:
        checks['cache'] = 'error'
    checks['scheduler'] = 'ok' if cache.get('ops:scheduler-heartbeat') else 'not running'
    try:
        checks['backups'] = backup_status()  # off-site copies; doesn't affect the status code
    except Exception:
        checks['backups'] = 'unknown'
    healthy = checks['database'] == 'ok' and checks['cache'] == 'ok'
    return JsonResponse({'status': 'ok' if healthy else 'error', **checks}, status=200 if healthy else 503)


@login_required
def business_details(request):
    """Settings → Business details (administrators): legal details, contact numbers and social links."""
    from audit import utils as audit

    from .forms import BusinessDetailsForm
    from .models import SiteSettings

    if not request.user.is_superuser:
        raise Http404
    form = BusinessDetailsForm(request.POST or None, instance=SiteSettings.objects.get_or_create(pk=1)[0])
    if request.method == 'POST' and form.is_valid():
        form.save()
        audit.record(owner=request.user, actor=request.user, action='site.details_changed',
                     summary='Changed business details: ' + ', '.join(form.changed_data or ['nothing']) + '.')
        messages.success(request, 'Business details saved.')
        return redirect('business_details')
    return render(request, 'ops/business_details.html', {'form': form})

