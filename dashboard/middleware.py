from urllib.parse import urlsplit
import hashlib
import re
import secrets

from django.conf import settings
from django.shortcuts import resolve_url
from django.utils.cache import add_never_cache_headers


ACTIVE_PROFILE_SESSION_KEY = 'active_repository_profile_id'
GUEST_MODE_SESSION_KEY = 'guest_mode'
GUEST_REPOSITORIES_SESSION_KEY = 'guest_repositories'
BROWSER_ACCOUNT_COOKIE = (
    '__Host-prdash_account_browser'
    if not settings.DEBUG or settings.SESSION_COOKIE_SECURE
    else 'prdash_account_browser'
)


def browser_key_hash(browser_key):
    return hashlib.sha256(browser_key.encode('utf-8')).hexdigest()


class BrowserAccountMiddleware:
    """Keep a browser-local list of GitHub accounts that have signed in here."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        static_path = f"/{urlsplit(settings.STATIC_URL).path.lstrip('/')}"
        if static_path and request.path.startswith(static_path):
            return self.get_response(request)

        browser_key = request.COOKIES.get(BROWSER_ACCOUNT_COOKIE)
        set_browser_cookie = not browser_key or not re.fullmatch(r'[A-Za-z0-9_-]{43}', browser_key)
        if set_browser_cookie:
            browser_key = secrets.token_urlsafe(32)

        request.account_browser_key = browser_key
        key_hash = browser_key_hash(browser_key)
        from .models import BrowserAccount

        if request.user.is_authenticated:
            BrowserAccount.objects.get_or_create(browser_key_hash=key_hash, user=request.user)
        request.browser_accounts = list(
            BrowserAccount.objects.filter(browser_key_hash=key_hash, user__is_active=True)
            .select_related('user')
        )
        response = self.get_response(request)
        if (
            response.get('Content-Type', '').startswith('text/html')
            and (
                request.user.is_authenticated
                or request.session.get(GUEST_MODE_SESSION_KEY)
                or request.browser_accounts
                or set_browser_cookie
            )
        ):
            add_never_cache_headers(response)
        if set_browser_cookie:
            response.set_cookie(
                BROWSER_ACCOUNT_COOKIE,
                browser_key,
                max_age=settings.SESSION_COOKIE_AGE,
                httponly=True,
                secure=not settings.DEBUG or settings.SESSION_COOKIE_SECURE,
                samesite='Lax',
                path='/',
            )
        return response


class RepositoryProfileMiddleware:
    """Attach the active repository profile to authenticated requests."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            from .models import RepositoryProfile

            profiles = RepositoryProfile.objects.filter(user=request.user)
            request.repository_profiles = profiles
            profile_selections = request.session.get(ACTIVE_PROFILE_SESSION_KEY)
            if isinstance(profile_selections, dict):
                profile_id = profile_selections.get(str(request.user.pk))
            else:
                profile_id = profile_selections
            profile = profiles.filter(id=profile_id).first() if profile_id else None
            if profile is None:
                profile = profiles.first()
            if profile is None:
                profile = RepositoryProfile.objects.create(user=request.user, name='Default')
            request.repository_profile = profile
            if not isinstance(profile_selections, dict):
                profile_selections = {}
            if profile_selections.get(str(request.user.pk)) != profile.id:
                profile_selections[str(request.user.pk)] = profile.id
                request.session[ACTIVE_PROFILE_SESSION_KEY] = profile_selections
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
