"""Speaking to tenants in their own language (English or Kiswahili).

The landlord side stays in English. Anything a tenant reads, the SMS we send them and the pages
behind their private link, is produced inside `translation.override(tenant.language)`.
"""

from functools import wraps

from django.utils import translation

from .models import Tenant


def tenant_language(tenant):
    """Context manager: translate everything inside into the tenant's language."""
    return translation.override(tenant.language if tenant else 'en')


def tenant_page(view):
    """Decorator for views reached by a tenant's private link (`token` argument)."""

    @wraps(view)
    def wrapper(request, token, *args, **kwargs):
        language = Tenant.objects.filter(portal_token=token).values_list('language', flat=True).first() or 'en'
        with translation.override(language):
            response = view(request, token, *args, **kwargs)
            if hasattr(response, 'render') and not response.is_rendered:
                response.render()
        response.headers.setdefault('Content-Language', language)
        return response

    return wrapper
