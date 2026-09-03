from allauth.socialaccount.models import SocialAccount
from django import template
from django.core.cache import cache
from django.utils import timezone

from dashboard.github_client import GITHUB_PROVIDER, USERNAME_CACHE_TTL_SECONDS

register = template.Library()


@register.filter
def time_ago(value):
    """Convert a datetime to a human-readable 'time ago' string."""
    if not value:
        return ""

    if isinstance(value, str):
        return value

    now = timezone.now()
    if timezone.is_naive(value):
        value = timezone.make_aware(value)

    diff = now - value

    seconds = diff.total_seconds()
    minutes = seconds / 60
    hours = minutes / 60
    days = hours / 24
    weeks = days / 7
    months = days / 30
    years = days / 365

    if seconds < 60:
        return "just now"
    elif minutes < 60:
        mins = int(minutes)
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    elif hours < 24:
        hrs = int(hours)
        return f"{hrs} hour{'s' if hrs != 1 else ''} ago"
    elif days < 7:
        d = int(days)
        return f"{d} day{'s' if d != 1 else ''} ago"
    elif days < 30:
        w = int(weeks)
        return f"{w} week{'s' if w != 1 else ''} ago"
    elif days < 365:
        m = int(months)
        return f"{m} month{'s' if m != 1 else ''} ago"
    else:
        y = int(years)
        return f"{y} year{'s' if y != 1 else ''} ago"


@register.simple_tag
def github_account_info(user):
    """Get the user's GitHub avatar/profile URLs from SocialAccount.extra_data (cached).

    Returns None if the user has no linked GitHub SocialAccount (e.g. PAT-only auth).
    """
    cache_key = f"github_account_info:{user.id}"
    cached = cache.get(cache_key, "__missing__")
    if cached != "__missing__":
        return cached

    info = None
    extra_data = SocialAccount.objects.filter(
        user=user,
        provider=GITHUB_PROVIDER,
    ).only('extra_data').values_list('extra_data', flat=True).first()
    if extra_data:
        info = {
            'avatar_url': extra_data.get('avatar_url'),
            'profile_url': extra_data.get('html_url'),
        }

    cache.set(cache_key, info, USERNAME_CACHE_TTL_SECONDS)
    return info


@register.filter
def is_light_color(hex_color):
    """Determine if a hex color is light (for choosing text color)."""
    if not hex_color:
        return True

    # Remove # if present
    hex_color = hex_color.lstrip('#')

    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
    except (ValueError, IndexError):
        return True

    # Calculate relative luminance
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255

    return luminance > 0.5


@register.filter
def hours_display(hours):
    """Convert hours to a human-readable display (e.g., '4d 5h' or '12h')."""
    if not hours or hours == 0:
        return "0h"

    try:
        hours = float(hours)
    except (ValueError, TypeError):
        return "0h"

    if hours >= 24:
        days = int(hours / 24)
        remaining_hours = int(hours % 24)
        if remaining_hours > 0:
            return f"{days}d {remaining_hours}h"
        return f"{days}d"
    else:
        return f"{int(hours)}h"
