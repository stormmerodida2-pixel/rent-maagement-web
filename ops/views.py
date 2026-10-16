from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
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
