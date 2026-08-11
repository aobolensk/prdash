from prdash.plugin_api import (
    HEADER_STATUS_SLOT,
    HEAD_SLOT,
    PLUGIN_API_VERSION,
    PluginMetadata,
    PluginTemplateResponse,
    TemplateResource,
    UIContribution,
)

from .status import get_github_status


PACKAGE = 'prdash_github_status'


class GitHubStatusPlugin:
    metadata = PluginMetadata(
        plugin_id='github-status',
        name='GitHub Status',
        version='1.1.0',
        api_version=PLUGIN_API_VERSION,
        description='Track GitHub API and pull request service availability.',
    )

    def initialize(self, registrar):
        registrar.register_ui(UIContribution(
            slot=HEAD_SLOT,
            template=TemplateResource(PACKAGE, 'templates/styles.html'),
        ))
        registrar.register_ui(UIContribution(
            slot=HEADER_STATUS_SLOT,
            template=TemplateResource(PACKAGE, 'templates/header.html'),
        ))
        registrar.register_route('status', self.status)
        registrar.register_service('status', get_github_status)

    def shutdown(self):
        pass

    @staticmethod
    def status(request, config):
        return PluginTemplateResponse(
            template=TemplateResource(PACKAGE, 'templates/status.html'),
            context={'status': get_github_status()},
        )


plugin = GitHubStatusPlugin()
