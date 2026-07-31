from django import template

from dashboard.plugin_manager import plugin_manager

register = template.Library()


@register.simple_tag(takes_context=True)
def plugin_slot(context, slot, **kwargs):
    """Render contributions from plugins enabled for the current user."""
    return plugin_manager.render_slot(slot, context, **kwargs)
