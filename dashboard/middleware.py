from urllib.parse import urlsplit

from django.conf import settings
from django.shortcuts import resolve_url


class HtmxLoginRedirectMiddleware:
    """Turn login redirects for HTMX requests into full-page navigations."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.headers.get('HX-Request') != 'true' or response.status_code != 302:
            return response

        login_path = urlsplit(resolve_url(settings.LOGIN_URL)).path
        redirect_path = urlsplit(response.get('Location', '')).path
        if redirect_path != login_path:
            return response

        response['HX-Redirect'] = response['Location']
        response.content = b''
        response.status_code = 200
        return response
