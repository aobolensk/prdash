from prdash.plugin_api import (
    HEAD_SLOT,
    HEADER_STATUS_SLOT,
    PLUGIN_API_VERSION,
    PluginMetadata,
    PluginTemplateResponse,
    TemplateResource,
    UIContribution,
)

from .stats_service import StatsService


PACKAGE = 'prdash_stats'


def _parse_days_param(value: str) -> int:
    """Parse the days parameter, returning -1 for 'all'."""
    if value == 'all':
        return -1
    try:
        days = int(value)
        if days not in (7, 14, 30, 90, 180, 365):
            return 30
        return days
    except (ValueError, TypeError):
        return 30


class StatsPlugin:
    metadata = PluginMetadata(
        plugin_id='stats',
        name='Stats',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
        description='PR velocity, review, health, and collaboration analytics.',
    )

    def initialize(self, registrar):
        self.registrar = registrar
        registrar.register_ui(UIContribution(
            slot=HEAD_SLOT,
            template=TemplateResource(PACKAGE, 'templates/head.html'),
        ))
        registrar.register_ui(UIContribution(
            slot=HEADER_STATUS_SLOT,
            template=TemplateResource(PACKAGE, 'templates/nav_link.html'),
        ))
        registrar.register_route('page', self.page)
        registrar.register_route('content', self.content)

    def shutdown(self):
        pass

    def page(self, request, config):
        repos = self.registrar.list_tracked_repositories(request.user_id)
        days = _parse_days_param(request.query_params.get('days', '30'))
        return PluginTemplateResponse(
            template=TemplateResource(PACKAGE, 'templates/page.html'),
            context={'days': days, 'repos': repos},
        )

    def content(self, request, config):
        repos = self.registrar.list_tracked_repositories(request.user_id, enabled_only=True)
        repo_tuples = [(repo['owner'], repo['name']) for repo in repos]
        days = _parse_days_param(request.query_params.get('days', '30'))
        stats_service = StatsService(self.registrar, request.user_id)
        all_stats = stats_service.get_all_stats(repo_tuples, days)
        return PluginTemplateResponse(
            template=TemplateResource(PACKAGE, 'templates/content.html'),
            context={
                'days': days,
                'quick_stats': all_stats['quick'],
                'velocity_stats': all_stats['velocity'],
                'review_stats': all_stats['reviews'],
                'health_stats': all_stats['health'],
                'repo_stats': all_stats['repos'],
                'collaboration_stats': all_stats['collaboration'],
                'repos': repos,
            },
        )


plugin = StatsPlugin()
