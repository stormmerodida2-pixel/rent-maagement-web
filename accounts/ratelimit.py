"""Small fixed-window rate limiter on Django's cache.

Use a shared cache (Redis/Memcached) in production; the default local-memory cache is per process.
"""

import hashlib

from django.core.cache import cache
from django.shortcuts import render


def client_ip(request):
    # Behind a reverse proxy, make sure REMOTE_ADDR is the real client (e.g. set it from
    # X-Forwarded-For in the proxy or a trusted middleware); never trust that header blindly here.
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
