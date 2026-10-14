"""Small fixed-window rate limiter on Django's cache.

Use a shared cache (Redis/Memcached) in production; the default local-memory cache is per process.
"""

import hashlib

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render


def client_ip(request):
    """The visitor's IP. Behind TRUSTED_PROXY_COUNT proxies, read it from X-Forwarded-For.

    Each proxy appends the address it received the request from, so the client is the entry
    TRUSTED_PROXY_COUNT places from the right. Anything further left was supplied by the client
    and can't be trusted (it could be forged to dodge the limits).
    """
    proxies = settings.TRUSTED_PROXY_COUNT
    if proxies > 0:
        hops = [h.strip() for h in request.META.get('HTTP_X_FORWARDED_FOR', '').split(',') if h.strip()]
        if len(hops) >= proxies:
            return hops[-proxies]
    return request.META.get('REMOTE_ADDR', 'unknown')


def _key(scope, ident):
    return f'rl:{scope}:{hashlib.sha256(str(ident).lower().encode()).hexdigest()[:32]}'


def is_limited(scope, ident, limit):
    return (cache.get(_key(scope, ident)) or 0) >= limit


def hit(scope, ident, window):
    """Record one attempt. Returns the count within the current window."""
    key = _key(scope, ident)
    if cache.add(key, 1, window):
        return 1
    try:
        return cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.add(key, 1, window)
        return 1


def reset(scope, ident):
    cache.delete(_key(scope, ident))


def too_many(request, minutes):
    return render(request, '429.html', {'minutes': minutes}, status=429)
