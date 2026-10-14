from django.contrib.sessions.middleware import SessionMiddleware as DjangoSessionMiddleware


class SessionMiddleware(DjangoSessionMiddleware):
    """Django's session middleware, except background requests can opt out of counting as activity.

    With SESSION_SAVE_EVERY_REQUEST every request pushes the idle-logout deadline back. A view
    sets request.passive_session = True (e.g. the dashboard's live polling) so that automatic
    background requests don't keep an unattended session signed in forever.
    """

    def process_response(self, request, response):
        if getattr(request, 'passive_session', False) and not request.session.modified:
            return response
        return super().process_response(request, response)
