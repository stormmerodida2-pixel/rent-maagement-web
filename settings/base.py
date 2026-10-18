"""
Settings shared by every environment. Don't point DJANGO_SETTINGS_MODULE here; use one of:

- settings.local        your computer (the default for manage.py)
- settings.development  a test copy online, for trying changes before they go live
- settings.production   the live site (the default for the server: wsgi.py and the Docker image)

Secrets and anything that differs between servers come from environment variables (loaded from .env
if present). See the "Deploying" section of README.md.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # the project folder

# Each settings file reads its own env file: local -> .env_local, development -> .env_dev,
# production -> .env_prod (ENV_FILE=path overrides). Variables already in the environment win, so on the
# server the values docker compose passes in are used as they are.
ENV_FILES = {'local': '.env_local', 'development': '.env_dev', 'production': '.env_prod'}
_settings_name = os.environ.get('DJANGO_SETTINGS_MODULE', 'settings.local').rsplit('.', 1)[-1]
load_dotenv(os.environ.get('ENV_FILE') or BASE_DIR / ENV_FILES.get(_settings_name, '.env_local'))


def env_bool(name, default=False):
    return os.environ.get(name, '1' if default else '0').strip().lower() in ('1', 'true', 'yes', 'on')


def env_list(name, default=''):
    return [item.strip() for item in os.environ.get(name, default).split(',') if item.strip()]


def require(name, why):
    """Stop with a clear message when a setting the server can't run without is missing."""
    if not os.environ.get(name):
        raise ImproperlyConfigured(f'{name} must be set: {why} See "First deploy" in README.md.')
    return os.environ[name]


# --- Core -----------------------------------------------------------------------------------

DEBUG = False  # local.py turns it on

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')  # required in production.py

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
    'billing',
    'announcements',
    'listings',
    'ops',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.csp.ContentSecurityPolicyMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'ops.utils.SessionMiddleware',  # Django's, plus passive background requests
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'accounts.utils.StaffAreaMiddleware',  # staff accounts only reach /staff/
    'django.contrib.messages.middleware.MessageMiddleware',
    'billing.utils.ReadOnlyWhenUnpaidMiddleware',  # landlords with an unpaid bill can look but not change
    'accounts.utils.UpdatedTermsMiddleware',  # landlords accept changed terms before going on
    'accounts.utils.ReadOnlyViewerMiddleware',  # co-owners and accountants see the landlord's account, read-only
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# Log in with an email address or phone number (older accounts: their username too).
AUTHENTICATION_BACKENDS = ['accounts.utils.EmailOrPhoneBackend']

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
                'rentals.utils.branding',
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
# The landlord side is in English. Tenant-facing SMS and pages use each tenant's language
# (Tenant.language), switched on per request/message rather than by LocaleMiddleware.
LANGUAGES = [('en', 'English'), ('sw', 'Kiswahili')]
LOCALE_PATHS = [BASE_DIR / 'locale']
TIME_ZONE = 'Africa/Nairobi'
USE_I18N = True
USE_TZ = True


# --- Static files (served by WhiteNoise, so no separate web server config is needed) -------------

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
# Uploaded files (listing photos). On the server they live on a Docker volume that Caddy serves.
MEDIA_URL = '/media/'
MEDIA_ROOT = Path(os.environ.get('MEDIA_ROOT', BASE_DIR / 'media'))
# Private uploads (receipts, meter photos): never served directly, only through a page that checks who's asking.
PRIVATE_MEDIA_ROOT = Path(os.environ.get('PRIVATE_MEDIA_ROOT', BASE_DIR / 'private_media'))

STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        # Hashed, compressed files that browsers can cache forever. Needs `collectstatic`, so
        # local.py uses the simple storage instead.
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}


# --- Security ---------------------------------------------------------------------------------
# (HTTPS-only cookies, redirects and HSTS are in production.py.)

# Behind a reverse proxy (Caddy, nginx, a load balancer) that terminates HTTPS:
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
# How many trusted proxies sit in front of the app. Client IPs (used for login/sign-up limits)
# are read from X-Forwarded-For only up to this many hops; 0 means use the direct connection.
TRUSTED_PROXY_COUNT = int(os.environ.get('TRUSTED_PROXY_COUNT', '0'))

# Content-Security-Policy: the browser only runs our own scripts (plus the one inline theme
# script, marked with a per-request nonce), so injected <script> tags and onclick= handlers do
# nothing. Inline style attributes are allowed: they can't run code. Set CSP_REPORT_ONLY=1 to
# trial a change: violations are reported in the browser console but nothing is blocked.
_csp = {
    'default-src': [CSP.SELF],
    'script-src': [CSP.SELF, CSP.NONCE],
    'style-src': [CSP.SELF, CSP.UNSAFE_INLINE],
    'font-src': [CSP.SELF],  # Inter is served from our own static files
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
    MAILERS = {'default': {'BACKEND': 'ops.mail.ReadableConsoleBackend'}}  # readable, links on one line
# Where Contact-page messages are emailed (the people running the site).
SUPPORT_EMAIL = os.environ.get('SUPPORT_EMAIL', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'Rentipay <no-reply@localhost>')
SERVER_EMAIL = DEFAULT_FROM_EMAIL


# --- App settings ---------------------------------------------------------------------------

# Product name shown across the site, home page and emails/SMS footers.
APP_NAME = os.environ.get('APP_NAME', 'Rentipay')

# Fallbacks for the privacy policy and terms; administrators set the real details in Settings → Business details.
LEGAL = {
    'ENTITY': os.environ.get('LEGAL_ENTITY_NAME', APP_NAME),
    'ADDRESS': os.environ.get('LEGAL_ADDRESS', ''),
    'EMAIL': os.environ.get('PRIVACY_EMAIL', '') or os.environ.get('SUPPORT_EMAIL', ''),
    # Bump TERMS_VERSION when the terms change materially; each landlord's accepted version is recorded.
    'UPDATED': 'October 16, 2026',
    'TERMS_VERSION': '2026-10-16',  # adds fees and billing
}

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

# Fernet key for encrypting landlords' Daraja credentials at rest (required in production.py).
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FIELD_ENCRYPTION_KEY = os.environ.get('FIELD_ENCRYPTION_KEY', '')

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
BACKUP_ENCRYPTION_KEY = os.environ.get('BACKUP_ENCRYPTION_KEY', '')  # required in production.py
# Off-site copies: each encrypted backup is also uploaded to this S3 bucket (ideally in another region),
# so losing the server doesn't lose the data. On EC2 the server's IAM role supplies credentials; it only
# needs s3:PutObject, and the bucket's lifecycle rule removes old copies. Empty = no off-site copies.
BACKUP_S3_BUCKET = os.environ.get('BACKUP_S3_BUCKET', '')
BACKUP_S3_REGION = os.environ.get('BACKUP_S3_REGION', '')
BACKUP_S3_PREFIX = os.environ.get('BACKUP_S3_PREFIX', 'backups/')


# --- Logging & error alerts ---------------------------------------------------------------------

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
# Errors (a page crashing, a scheduled job failing) also go to the site owner: a phone push to this ntfy topic
# (the server's ~/.config/liskodi/alerts.env has the same one) and an email with the details. See ops/alerts.py.
ALERT_NTFY_URL = os.environ.get('ALERT_NTFY_URL', '')
ALERT_EMAIL = os.environ.get('ALERT_EMAIL', '')
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'plain': {'format': '%(asctime)s %(levelname)s %(name)s: %(message)s'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'plain'},
                 'alerts': {'class': 'ops.alerts.ErrorAlertHandler', 'level': 'ERROR'}},
    'root': {'handlers': ['console', 'alerts'], 'level': 'WARNING'},
    'loggers': {
        'django': {'handlers': ['console', 'alerts'], 'level': 'WARNING', 'propagate': False},
        **{app: {'handlers': ['console', 'alerts'], 'level': LOG_LEVEL, 'propagate': False}
           for app in ('payments', 'notifications', 'accounts', 'support', 'ops', 'billing')},
    },
}



def init_error_alerts(environment):
    """Set SENTRY_DSN to get an email/notification for every server error (called by the server settings)."""
    if os.environ.get('SENTRY_DSN'):
        import sentry_sdk

        sentry_sdk.init(
            dsn=os.environ['SENTRY_DSN'],
            environment=os.environ.get('SENTRY_ENVIRONMENT', environment),
            traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '0')),
            send_default_pii=False,  # don't send tenants' personal data to a third party
        )
