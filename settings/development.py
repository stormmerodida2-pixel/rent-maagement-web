"""A test copy of the app online (staging), for trying changes with real phones before they go live.

Same security as production (HTTPS, secret keys required), but it's labelled "(test)" across the site so
nobody mistakes it for the live one, browsers aren't told to remember HTTPS for long, and error alerts
are tagged "development". Use M-Pesa sandbox credentials and the Africa's Talking sandbox here.
Select it on that server with DJANGO_SETTINGS_MODULE=settings.development.
"""

import os

from .production import *  # noqa: F403

APP_NAME = os.environ.get('APP_NAME', 'Rentipay (test)')

# A test domain may be torn down or moved; don't make browsers insist on HTTPS for it for long.
SECURE_HSTS_SECONDS = int(os.environ.get('DJANGO_HSTS_SECONDS', '0'))
