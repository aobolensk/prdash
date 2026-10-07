from prdash.plugin_api import (
    HEAD_SLOT,
    PLUGIN_API_VERSION,
    PR_CARD_ACTIONS_SLOT,
    PR_LIST_FILTERS_SLOT,
    PluginMetadata,
    PluginTemplateResponse,
    TemplateResource,
    UIContribution,
)


PACKAGE = 'prdash_export_links'
SETTINGS_TEMPLATE = TemplateResource(PACKAGE, 'templates/settings.html')
MAX_FORMAT_LENGTH = 200
SEPARATORS = {
    'newline': ('New line', '\n'),
    'comma': ('Comma', ', '),
    'space': ('Space', ' '),
}
DEFAULTS = {
    'single_format': '{url}',
    'list_format': '{url}',
    'separator': 'newline',
}
PLACEHOLDERS = ('url', 'title', 'number', 'repo', 'author', 'branch')


class ExportLinksPlugin:
    metadata = PluginMetadata(
        plugin_id='export-links',
        name='Export Links',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
        description=(
            'Export pull request links to the clipboard, one at a time or all visible ones, '
            'in a configurable format.'
        ),
    )

    def initialize(self, registrar):
        self.registrar = registrar
        registrar.register_ui(UIContribution(
            slot=HEAD_SLOT,
            template=TemplateResource(PACKAGE, 'templates/head.html'),
            context_provider=self.head_context,
        ))
        registrar.register_ui(UIContribution(
            slot=PR_CARD_ACTIONS_SLOT,
            template=TemplateResource(PACKAGE, 'templates/export_button.html'),
        ))
        registrar.register_ui(UIContribution(
            slot=PR_LIST_FILTERS_SLOT,
            template=TemplateResource(PACKAGE, 'templates/export_all_button.html'),
            order=1000,
        ))
        registrar.register_ui(UIContribution(
            slot='settings',
            template=SETTINGS_TEMPLATE,
            context_provider=self.settings_context,
        ))
        registrar.register_route('settings', self.settings)

    def shutdown(self):
        pass

    @staticmethod
    def _formats(config):
        values = {**DEFAULTS, **{key: config[key] for key in DEFAULTS if config.get(key)}}
        if values['separator'] not in SEPARATORS:
            values['separator'] = DEFAULTS['separator']
        return values

    def head_context(self, request, config):
        formats = self._formats(config)
        return {
            'export_links_formats': {
                'single': formats['single_format'],
                'list': formats['list_format'],
                'separator': SEPARATORS[formats['separator']][1],
            },
        }

    def settings_context(self, request, config, success='', error=''):
        formats = self._formats(config)
        return {
            'export_links_single_format': formats['single_format'],
            'export_links_list_format': formats['list_format'],
            'export_links_separators': [
                {'key': key, 'label': label, 'selected': key == formats['separator']}
                for key, (label, _) in SEPARATORS.items()
            ],
            'export_links_placeholders': ' '.join('{%s}' % name for name in PLACEHOLDERS),
            'export_links_success': success,
            'export_links_error': error,
        }

    def settings(self, request, config):
        if request.method != 'POST':
            return PluginTemplateResponse(
                template=SETTINGS_TEMPLATE,
                context=self.settings_context(request, config, error='Method not allowed.'),
                status=405,
            )
        if request.user_id is None:
            return PluginTemplateResponse(
                template=SETTINGS_TEMPLATE,
                context=self.settings_context(request, config, error='Authentication required.'),
                status=401,
            )

        single_format = request.form_params.get('single_format', '').strip()
        list_format = request.form_params.get('list_format', '').strip()
        separator = request.form_params.get('separator', '')
        if (
            not single_format or not list_format
            or len(single_format) > MAX_FORMAT_LENGTH or len(list_format) > MAX_FORMAT_LENGTH
            or separator not in SEPARATORS
        ):
            return PluginTemplateResponse(
                template=SETTINGS_TEMPLATE,
                context=self.settings_context(
                    request, config,
                    error=f'Formats must be 1 to {MAX_FORMAT_LENGTH} characters.',
                ),
            )

        values = {
            **self.registrar.get_user_config(request.user_id),
            'single_format': single_format,
            'list_format': list_format,
            'separator': separator,
        }
        self.registrar.update_user_config(request.user_id, values)
        return PluginTemplateResponse(
            template=SETTINGS_TEMPLATE,
            context=self.settings_context(request, values, success='Export Links settings saved.'),
        )


plugin = ExportLinksPlugin()
