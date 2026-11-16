from django.apps import AppConfig


class DashboardConfig(AppConfig):
    name = 'dashboard'

    def ready(self):
        from . import signals  # noqa: F401
        from .plugin_manager import plugin_manager
        plugin_manager.discover()
