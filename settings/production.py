"""The live site. The default on the server (wsgi.py, asgi.py and the Docker image).

Debug is off, HTTPS is enforced, and the app refuses to start without its secret keys.
"""

from .base import *  # noqa: F403
from .base import env_bool, init_error_alerts, require

DEBUG = False

SECRET_KEY = require('DJANGO_SECRET_KEY', 'it signs logins and links. Generate one with '
                     'python -c "import secrets; print(secrets.token_urlsafe(50))".')
FIELD_ENCRYPTION_KEY = require('FIELD_ENCRYPTION_KEY',
                               "it encrypts stored M-Pesa credentials, so they don't depend on DJANGO_SECRET_KEY.")
BACKUP_ENCRYPTION_KEY = require('BACKUP_ENCRYPTION_KEY', 'it encrypts the nightly database backups.')

# HTTPS (Caddy in front terminates it; see SECURE_PROXY_SSL_HEADER in base.py).
SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL_REDIRECT', True)
SECURE_REDIRECT_EXEMPT = [r'^healthz/$']  # load balancers probe over plain HTTP
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# Start small; raise to 31536000 (a year) once HTTPS is confirmed working.
SECURE_HSTS_SECONDS = int(os.environ.get('DJANGO_HSTS_SECONDS', '3600'))  # noqa: F405
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('DJANGO_HSTS_INCLUDE_SUBDOMAINS')
SECURE_HSTS_PRELOAD = env_bool('DJANGO_HSTS_PRELOAD')

# Tagged with the settings in use: "production", or "development" on the test copy (which builds on this file).
init_error_alerts(os.environ.get('DJANGO_SETTINGS_MODULE', 'settings.production').rsplit('.', 1)[-1])  # noqa: F405
