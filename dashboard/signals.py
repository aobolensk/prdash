from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from allauth.socialaccount.signals import pre_social_login

from .middleware import (
    ACTIVE_PROFILE_SESSION_KEY,
    GUEST_MODE_SESSION_KEY,
    GUEST_REPOSITORIES_SESSION_KEY,
    browser_key_hash,
)
from .models import BrowserAccount


@receiver(pre_social_login)
def remember_previous_browser_account(request, sociallogin, **kwargs):
    """Remember the signed-in identity before allauth switches to another one."""
    request.prdash_preserved_browser_state = {
        key: request.session[key]
        for key in (ACTIVE_PROFILE_SESSION_KEY, GUEST_REPOSITORIES_SESSION_KEY)
        if key in request.session
    }
    if request.user.is_authenticated and request.user.pk != sociallogin.user.pk:
        request.prdash_previous_browser_account_id = request.user.pk


@receiver(user_logged_in)
def save_browser_account(request, user, **kwargs):
    """Make successfully authenticated GitHub accounts available in this browser."""
    browser_key = getattr(request, 'account_browser_key', None)
    if not browser_key:
        return

    key_hash = browser_key_hash(browser_key)
    user_ids = {user.pk}
    previous_user_id = getattr(request, 'prdash_previous_browser_account_id', None)
    if previous_user_id:
        user_ids.add(previous_user_id)

    for user_id in user_ids:
        BrowserAccount.objects.get_or_create(browser_key_hash=key_hash, user_id=user_id)
    for key, value in getattr(request, 'prdash_preserved_browser_state', {}).items():
        request.session[key] = value
    request.session.pop(GUEST_MODE_SESSION_KEY, None)
