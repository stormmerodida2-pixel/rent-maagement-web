"""Helpers for the staff area."""

from django.shortcuts import redirect

from .models import staff_for


# --- Middleware --------------------------------------------------------------------------------------

# Everything else (dashboard, reports, payments, settings…) is off limits to staff accounts.
STAFF_ALLOWED = ('/staff/', '/logout/', '/login/', '/static/', '/sw.js', '/manifest.webmanifest', '/offline/',
                 '/privacy/', '/terms/', '/healthz/', '/robots.txt', '/t/')


class StaffAreaMiddleware:
    """Keep staff accounts inside the staff area: deny by default, so new landlord pages are covered too."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.staff = staff_for(request.user)
        if request.staff is not None and not request.path.startswith(STAFF_ALLOWED):
            return redirect('staff:home')
        return self.get_response(request)
