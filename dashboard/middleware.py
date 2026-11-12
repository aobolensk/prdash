from urllib.parse import urlsplit

from django.conf import settings
from django.shortcuts import resolve_url


ACTIVE_PROFILE_SESSION_KEY = 'active_repository_profile_id'


class RepositoryProfileMiddleware:
    """Attach the active repository profile to authenticated requests."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            from .models import RepositoryProfile

            profiles = RepositoryProfile.objects.filter(user=request.user)
            request.repository_profiles = profiles
            profile_id = request.session.get(ACTIVE_PROFILE_SESSION_KEY)
            profile = profiles.filter(id=profile_id).first() if profile_id else None
            if profile is None:
                profile = profiles.first()
            if profile is None:
                profile = RepositoryProfile.objects.create(user=request.user, name='Default')
            request.repository_profile = profile
            if request.session.get(ACTIVE_PROFILE_SESSION_KEY) != profile.id:
                request.session[ACTIVE_PROFILE_SESSION_KEY] = profile.id
        return self.get_response(request)


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
