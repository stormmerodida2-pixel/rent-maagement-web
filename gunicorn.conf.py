"""gunicorn settings for production (used by the Dockerfile)."""

import multiprocessing
import os

bind = '0.0.0.0:8000'
workers = int(os.environ.get('WEB_CONCURRENCY', min(multiprocessing.cpu_count() * 2 + 1, 4)))
# M-Pesa and SMS calls can take up to ~30s; give requests room before killing a worker.
timeout = int(os.environ.get('GUNICORN_TIMEOUT', '60'))
graceful_timeout = 30
max_requests = 1000          # recycle workers now and then to keep memory steady
max_requests_jitter = 100
accesslog = '-'
errorlog = '-'
# Only Caddy can reach this port (it isn't published), so trust its forwarded headers.
forwarded_allow_ips = '*'
