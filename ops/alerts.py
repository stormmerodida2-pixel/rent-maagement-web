"""Tell the site owner when something goes wrong inside the app: a page crashing for a landlord or tenant, or a
scheduled job failing. A phone push (ntfy, ALERT_NTFY_URL) and an email with the details (ALERT_EMAIL).

The same problem alerts at most once an hour. The push says only what broke and where, never names or numbers,
because anyone who knows the ntfy topic can read it; the email (to you only) has the traceback.
"""

import hashlib
import logging
import threading
import traceback

REPEAT_AFTER = 60 * 60  # seconds
_sending = threading.local()


def _where(record):
    request = getattr(record, 'request', None)
    path = getattr(request, 'path', None)
    return path or f'{record.name} ({record.funcName})'


def _kind(record):
    if record.exc_info and record.exc_info[0]:
        return record.exc_info[0].__name__
    return record.getMessage().split(':', 1)[0][:80]


class ErrorAlertHandler(logging.Handler):
    def __init__(self, level=logging.ERROR):
        super().__init__(level)

    def emit(self, record):
        if getattr(_sending, 'active', False):  # an alert failing must not alert about itself
            return
        if record.name.startswith('django.security'):  # bots sending bad Host headers etc.: noise, not breakage
            return
        _sending.active = True
        try:
            self._alert(record)
        except Exception:
            pass  # alerts are best effort: never break the request or job that hit the error
        finally:
            _sending.active = False

    def _alert(self, record):
        from django.conf import settings
        from django.core.cache import cache

        ntfy, email = getattr(settings, 'ALERT_NTFY_URL', ''), getattr(settings, 'ALERT_EMAIL', '')
        if not (ntfy or email):
            return
        kind, where = _kind(record), _where(record)
        key = 'ops:alert:' + hashlib.sha256(f'{record.name}|{kind}|{where}'.encode()).hexdigest()[:32]
        if not cache.add(key, 1, REPEAT_AFTER):
            return
        title = f'{settings.APP_NAME}: {kind}'
        short = f'{kind} at {where}. Details sent by email; the same error won\'t alert again for an hour.'
        if ntfy:
            import requests

            requests.post(ntfy, data=short.encode(), timeout=5,
                          headers={'Title': title, 'Priority': 'high', 'Tags': 'rotating_light'})
        if email:
            from django.core.mail import send_mail

            details = ''.join(traceback.format_exception(*record.exc_info)) if record.exc_info else ''
            request = getattr(record, 'request', None)
            user = getattr(request, 'user', None)
            send_mail(
                subject=f'[{settings.APP_NAME} error] {kind} at {where}'[:150],
                message=(f'{record.getMessage()}\n\nWhere: {where}\n'
                         + (f'Method: {request.method}\n' if request is not None else '')
                         + (f'User id: {user.pk}\n' if getattr(user, 'pk', None) else '')
                         + f'Logger: {record.name}\n\n{details}\n'
                         'The same error at the same place will not be emailed again for an hour.'),
                from_email=None, recipient_list=[email], fail_silently=True,
            )
