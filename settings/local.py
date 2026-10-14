"""Your computer. The default for `python manage.py …`, and what the tests run with.

Debug pages are on, HTTPS isn't needed, and emails and SMS are printed to the terminal unless you set
EMAIL_HOST / SMS_BACKEND in .env. Without DATABASE_URL it uses a local SQLite file.
"""

from .base import *  # noqa: F403

DEBUG = True

SECRET_KEY = SECRET_KEY or 'django-insecure-local-only'  # noqa: F405

# Serve CSS/JS straight from static/, without needing `collectstatic`.
STORAGES = {**STORAGES, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}  # noqa: F405
