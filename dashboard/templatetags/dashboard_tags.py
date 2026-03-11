from django import template
from django.utils import timezone
from datetime import datetime

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
    elif weeks < 4:
        w = int(weeks)
        return f"{w} week{'s' if w != 1 else ''} ago"
    elif months < 12:
        m = int(months)
        return f"{m} month{'s' if m != 1 else ''} ago"
    else:
        y = int(years)
        return f"{y} year{'s' if y != 1 else ''} ago"


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
