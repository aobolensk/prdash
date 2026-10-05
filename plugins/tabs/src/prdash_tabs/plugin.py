from importlib import resources

from prdash.plugin_api import (
    HEAD_SLOT,
    PLUGIN_API_VERSION,
    PluginMetadata,
    TemplateResource,
    UIContribution,
)


PACKAGE = 'prdash_tabs'


class TabsPlugin:
    metadata = PluginMetadata(
        plugin_id='tabs',
        name='Tabs',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
        description='Adds tabs to the PR dashboard.',
    )

    def initialize(self, registrar):
        registrar.register_ui(UIContribution(
            slot=HEAD_SLOT,
            template=TemplateResource(PACKAGE, 'templates/head.html'),
            context_provider=self.head_context,
        ))

    def head_context(self, request, config):
        if not getattr(request, 'path', '').startswith('/prs'):
            return {}
        assets = resources.files(PACKAGE).joinpath('static')
        return {
            'tabs_css': assets.joinpath('tabs.css').read_text(encoding='utf-8'),
            'tabs_js': assets.joinpath('tabs.js').read_text(encoding='utf-8'),
        }

    def shutdown(self):
        pass


plugin = TabsPlugin()
