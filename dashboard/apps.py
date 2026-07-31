from django.apps import AppConfig


class DashboardConfig(AppConfig):
    name = 'dashboard'

    def ready(self):
        from .plugin_manager import plugin_manager
        plugin_manager.discover()
