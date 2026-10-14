"""
Django settings for the rental manager.

Everything that differs between a laptop and a server comes from environment variables
(loaded from .env if present). Defaults are safe for production: DEBUG is off unless
DJANGO_DEBUG=1, and a real DJANGO_SECRET_KEY is required when it is off.
See .env.example and the "Deploying" section of README.md.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


def env_bool(name, default=False):
    return os.environ.get(name, '1' if default else '0').strip().lower() in ('1', 'true', 'yes', 'on')


def env_list(name, default=''):
    return [item.strip() for item in os.environ.get(name, default).split(',') if item.strip()]


# --- Core -----------------------------------------------------------------------------------

DEBUG = env_bool('DJANGO_DEBUG')

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured(
            'DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off. Generate one with: '
            'python -c "import secrets; print(secrets.token_urlsafe(50))"'
        )
    SECRET_KEY = 'django-insecure-dev-only-change-me'

# Public base URL of this site, used in links sent to tenants (SMS, portal, emails).
SITE_URL = os.environ.get('SITE_URL', 'http://localhost:8000').rstrip('/')
_site = urlsplit(SITE_URL)

# The site's own host is always allowed; add others (e.g. www.) with DJANGO_ALLOWED_HOSTS.
# localhost stays allowed so container health checks can reach /healthz/.
ALLOWED_HOSTS = list(dict.fromkeys(env_list('DJANGO_ALLOWED_HOSTS') + [_site.hostname, 'localhost', '127.0.0.1']))
CSRF_TRUSTED_ORIGINS = list(dict.fromkeys(
    env_list('DJANGO_CSRF_TRUSTED_ORIGINS') + [f'{_site.scheme}://{_site.netloc}']
))


# --- Application definition -----------------------------------------------------------------

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'django.contrib.sitemaps',
    'rentals',
    'payments',
    'maintenance',
    'notifications',
    'accounts',
    'reports',
    'audit',
    'support',
    'announcements',
    'ops',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.csp.ContentSecurityPolicyMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'ops.middleware.SessionMiddleware',  # Django's, plus passive background requests
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.template.context_processors.csp',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'rentals.context_processors.branding',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# --- Database & cache -------------------------------------------------------------------------

# DATABASE_URL, e.g. postgres://user:password@db:5432/rentals. Without it, a local SQLite file.
DATABASES = {
    'default': dj_database_url.parse(
        os.environ.get('DATABASE_URL') or f'sqlite:///{BASE_DIR / "db.sqlite3"}',  # empty counts as unset
        conn_max_age=600,
        conn_health_checks=True,
    ),
}

# REDIS_URL, e.g. redis://redis:6379/0. Needed in production so login/sign-up limits and
# M-Pesa tokens are shared by every web process. Without it, per-process memory.
if os.environ.get('REDIS_URL'):
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.redis.RedisCache',
                          'LOCATION': os.environ['REDIS_URL']}}
else:
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# --- Passwords ------------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'rentals:dashboard'
LOGOUT_REDIRECT_URL = 'rentals:home'


# --- Internationalization -------------------------------------------------------------------

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Nairobi'
USE_I18N = True
USE_TZ = True


# --- Static files (served by WhiteNoise, so no separate web server config is needed) -------------

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        # Hashed, compressed files that browsers can cache forever. Needs `collectstatic`, so
        # plain local development (DEBUG on) keeps the simple storage.
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage' if DEBUG
        else 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}


# --- Security (production) ----------------------------------------------------------------------

# Behind a reverse proxy (Caddy, nginx, a load balancer) that terminates HTTPS:
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
# How many trusted proxies sit in front of the app. Client IPs (used for login/sign-up limits)
# are read from X-Forwarded-For only up to this many hops; 0 means use the direct connection.
TRUSTED_PROXY_COUNT = int(os.environ.get('TRUSTED_PROXY_COUNT', '0'))

if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL_REDIRECT', True)
    SECURE_REDIRECT_EXEMPT = [r'^healthz/$']  # load balancers probe over plain HTTP
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Start small; raise to 31536000 (a year) once HTTPS is confirmed working.
    SECURE_HSTS_SECONDS = int(os.environ.get('DJANGO_HSTS_SECONDS', '3600'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('DJANGO_HSTS_INCLUDE_SUBDOMAINS')
    SECURE_HSTS_PRELOAD = env_bool('DJANGO_HSTS_PRELOAD')

# Content-Security-Policy: the browser only runs our own scripts (plus the one inline theme
# script, marked with a per-request nonce), so injected <script> tags and onclick= handlers do
# nothing. Inline style attributes are allowed: they can't run code. Set CSP_REPORT_ONLY=1 to
# trial a change: violations are reported in the browser console but nothing is blocked.
_csp = {
    'default-src': [CSP.SELF],
    'script-src': [CSP.SELF, CSP.NONCE],
    'style-src': [CSP.SELF, CSP.UNSAFE_INLINE, 'https://fonts.googleapis.com'],
    'font-src': [CSP.SELF, 'https://fonts.gstatic.com'],
    'img-src': [CSP.SELF, 'data:'],
    'connect-src': [CSP.SELF],
    'object-src': [CSP.NONE],
    'base-uri': [CSP.SELF],
    'form-action': [CSP.SELF],
    'frame-ancestors': [CSP.NONE],
}
if env_bool('CSP_REPORT_ONLY'):
    SECURE_CSP_REPORT_ONLY = _csp
else:
    SECURE_CSP = _csp

SESSION_COOKIE_HTTPONLY = True
# Log people out after a period of inactivity (default 60 minutes). Every request pushes the
# expiry back, so active users are never interrupted. Sessions are cached to keep that cheap.
SESSION_COOKIE_AGE = int(os.environ.get('SESSION_IDLE_MINUTES', '60')) * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_ENGINE = 'django.contrib.sessions.backends.cached_db'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'


# --- Email ------------------------------------------------------------------------------------

# Printed to the console unless EMAIL_HOST is set (e.g. smtp.sendgrid.net, smtp.gmail.com).
if os.environ.get('EMAIL_HOST'):
    MAILERS = {
        'default': {
            'BACKEND': 'django.core.mail.backends.smtp.EmailBackend',
            'OPTIONS': {
                'host': os.environ['EMAIL_HOST'],
                'port': int(os.environ.get('EMAIL_PORT', '587')),
                'username': os.environ.get('EMAIL_HOST_USER', ''),
                'password': os.environ.get('EMAIL_HOST_PASSWORD', ''),
                'use_tls': env_bool('EMAIL_USE_TLS', True),
                'timeout': 20,
            },
        },
    }
else:
    MAILERS = {'default': {'BACKEND': 'django.core.mail.backends.console.EmailBackend'}}
# Where Contact-page messages are emailed (the people running the site).
SUPPORT_EMAIL = os.environ.get('SUPPORT_EMAIL', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'Liskodi <no-reply@localhost>')
SERVER_EMAIL = DEFAULT_FROM_EMAIL


# --- App settings ---------------------------------------------------------------------------

# Product name shown across the site, home page and emails/SMS footers.
APP_NAME = os.environ.get('APP_NAME', 'Liskodi')

# M-Pesa Daraja. Each landlord connects their own Paybill/Till and Daraja app in the
# app (Settings -> M-Pesa); only platform-wide values live here.
MPESA = {
    # Public HTTPS base URL Safaricom can reach for webhooks. Defaults to SITE_URL.
    'CALLBACK_BASE_URL': os.environ.get('MPESA_CALLBACK_BASE_URL', SITE_URL if _site.scheme == 'https' else ''),
    # Addresses Safaricom sends production callbacks from (IPs or CIDR ranges), per Safaricom's
    # published list. Callbacks from elsewhere aren't trusted: Paybill payments are held for the
    # landlord to review and STK results are confirmed with Safaricom instead. "any" turns the
    # check off. Sandbox accounts are never checked.
    'CALLBACK_IPS': env_list('MPESA_CALLBACK_IPS', ','.join([
        '196.201.214.200', '196.201.214.206', '196.201.213.114', '196.201.214.207',
        '196.201.214.208', '196.201.213.44', '196.201.212.127', '196.201.212.138',
        '196.201.212.129', '196.201.212.136', '196.201.212.74', '196.201.212.69',
    ])),
}

# Fernet key for encrypting landlords' Daraja credentials at rest.
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FIELD_ENCRYPTION_KEY = os.environ.get('FIELD_ENCRYPTION_KEY', '')
if not DEBUG and not FIELD_ENCRYPTION_KEY:
    raise ImproperlyConfigured(
        'FIELD_ENCRYPTION_KEY must be set when DJANGO_DEBUG is off, so stored M-Pesa credentials '
        "don't depend on DJANGO_SECRET_KEY. See .env.example for how to generate one."
    )

# SMS via Africa's Talking. 'console' just logs messages (development).
SMS = {
    'BACKEND': os.environ.get('SMS_BACKEND', 'console'),  # 'console' or 'africastalking'
    'AT_USERNAME': os.environ.get('AT_USERNAME', 'sandbox'),
    'AT_API_KEY': os.environ.get('AT_API_KEY', ''),
    'AT_SENDER_ID': os.environ.get('AT_SENDER_ID', ''),  # optional alphanumeric sender ID
    'PAYMENT_RECEIPTS': env_bool('SMS_PAYMENT_RECEIPTS', True),
}
# Shown as an estimate before sending announcements (KES per SMS part).
SMS_COST_PER_PART = float(os.environ.get('SMS_COST_PER_PART', '0.80'))

# Database backups written by the scheduler (`python manage.py backup_db`).
BACKUP_DIR = Path(os.environ.get('BACKUP_DIR', BASE_DIR / 'backups'))
BACKUP_KEEP_DAYS = int(os.environ.get('BACKUP_KEEP_DAYS', '14'))
# Fernet key that encrypts every backup file. Keep it somewhere other than the server (a password
# manager): it's what lets you read a backup after losing the server. Separate from
# FIELD_ENCRYPTION_KEY so backups can be stored by someone who can't read M-Pesa credentials.
BACKUP_ENCRYPTION_KEY = os.environ.get('BACKUP_ENCRYPTION_KEY', '')
if not DEBUG and not BACKUP_ENCRYPTION_KEY:
    raise ImproperlyConfigured(
        'BACKUP_ENCRYPTION_KEY must be set when DJANGO_DEBUG is off, so backups are encrypted. '
        'Generate it the same way as FIELD_ENCRYPTION_KEY (see .env.example).'
    )


# --- Logging & error alerts ---------------------------------------------------------------------

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'plain': {'format': '%(asctime)s %(levelname)s %(name)s: %(message)s'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'plain'}},
    'root': {'handlers': ['console'], 'level': 'WARNING'},
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        **{app: {'handlers': ['console'], 'level': LOG_LEVEL, 'propagate': False}
           for app in ('payments', 'notifications', 'accounts', 'support', 'ops')},
    },
}

# Error alerts: set SENTRY_DSN to get an email/notification for every server error.
if os.environ.get('SENTRY_DSN'):
    import sentry_sdk

    sentry_sdk.init(
        dsn=os.environ['SENTRY_DSN'],
        environment=os.environ.get('SENTRY_ENVIRONMENT', 'production'),
        traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '0')),
        send_default_pii=False,  # don't send tenants' personal data to a third party
    )
