import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from dashboard.models import PluginConfiguration
from dashboard.plugin_manager import PluginDescriptor, PluginManager, plugin_manager
from prdash.plugin_api import (
    PLUGIN_API_VERSION,
    PluginDependency,
    PluginMetadata,
    PullRequestListContext,
    PullRequestQuery,
)


class PluginDiscoveryTests(TestCase):
    def test_source_discovery_does_not_import_plugin(self):
        with TemporaryDirectory() as directory:
            plugin_directory = Path(directory) / 'example'
            plugin_directory.mkdir()
            manifest = {
                'id': 'example',
                'name': 'Example',
                'version': '1.2.3',
                'api_version': PLUGIN_API_VERSION,
                'entrypoint': 'module_that_must_not_load:plugin',
            }
            (plugin_directory / 'prdash-plugin.json').write_text(
                json.dumps(manifest),
                encoding='utf-8',
            )

            with override_settings(PRDASH_PLUGIN_PATHS=[directory]):
                manager = PluginManager()
                manager.discover()

            self.assertIn('example', manager.descriptors)
            self.assertNotIn('module_that_must_not_load', __import__('sys').modules)

    def test_incompatible_source_is_discovered_but_not_loaded(self):
        manager = PluginManager()
        manager.descriptors['example'] = PluginDescriptor(
            plugin_id='example',
            name='Example',
            version='1.0.0',
            description='',
            entrypoint='unused:plugin',
            source='test',
            api_version='2.0',
        )

        loaded = manager.load('example', {'example'})

        self.assertIsNone(loaded)
        self.assertIn('Requires plugin API 2.0', manager.descriptors['example'].load_error)


class _HookPlugin:
    metadata = PluginMetadata(
        plugin_id='hook-plugin',
        name='Hook Plugin',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
    )

    def __init__(self, callback):
        self.callback = callback
        self.shutdown_called = False

    def initialize(self, registrar):
        registrar.register_hook('example', self.callback)
        registrar.register_route('hello', self.hello)
        registrar.register_service('value', 42)

    def shutdown(self):
        self.shutdown_called = True

    @staticmethod
    def hello(request, config):
        return HttpResponse('hello')


class _DependencyPlugin:
    metadata = PluginMetadata(
        plugin_id='dependency',
        name='Dependency',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
    )

    def initialize(self, registrar):
        pass

    def shutdown(self):
        pass


class _DependentPlugin:
    metadata = PluginMetadata(
        plugin_id='dependent',
        name='Dependent',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
        dependencies=(PluginDependency('dependency', '>=1,<2'),),
    )

    def initialize(self, registrar):
        registrar.register_hook('example', self.increment)

    def shutdown(self):
        pass

    @staticmethod
    def increment(value, context, config):
        return value + 1


class PluginRuntimeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.factory = RequestFactory()

    @staticmethod
    def manager_with_plugin(plugin):
        manager = PluginManager()
        manager.descriptors['hook-plugin'] = PluginDescriptor(
            plugin_id='hook-plugin',
            name='Hook Plugin',
            version='1.0.0',
            description='',
            entrypoint='unused:plugin',
            source='test',
        )
        manager._load_object = MagicMock(return_value=plugin)
        return manager

    def test_plugin_only_executes_after_user_enables_it(self):
        plugin = _HookPlugin(lambda value, context, config: value + 1)
        manager = self.manager_with_plugin(plugin)

        disabled = manager.run_hook('example', 1, None, self.user)
        manager.configure_user(self.user, {'hook-plugin'})
        enabled = manager.run_hook('example', 1, None, self.user)

        self.assertEqual(disabled, 1)
        self.assertEqual(enabled, 2)

    def test_hook_failure_is_isolated(self):
        def broken_hook(value, context, config):
            raise RuntimeError('broken')

        manager = self.manager_with_plugin(_HookPlugin(broken_hook))
        manager.configure_user(self.user, {'hook-plugin'})

        result = manager.run_hook('example', 'unchanged', None, self.user)

        self.assertEqual(result, 'unchanged')

    def test_disabling_last_user_shuts_plugin_down(self):
        plugin = _HookPlugin(lambda value, context, config: value)
        manager = self.manager_with_plugin(plugin)
        manager.configure_user(self.user, {'hook-plugin'})

        manager.configure_user(self.user, set())

        self.assertTrue(plugin.shutdown_called)
        self.assertNotIn('hook-plugin', manager._loaded)

    def test_enabled_plugin_route_and_service_are_available(self):
        plugin = _HookPlugin(lambda value, context, config: value)
        manager = self.manager_with_plugin(plugin)
        manager.configure_user(self.user, {'hook-plugin'})
        request = self.factory.get('/plugins/hook-plugin/hello/')
        request.user = self.user

        response = manager.dispatch(request, 'hook-plugin', 'hello')

        self.assertEqual(response.content, b'hello')
        self.assertEqual(
            manager.get_service(self.user, 'hook-plugin', 'value'),
            42,
        )

    def test_dependency_must_remain_enabled_for_plugin_execution(self):
        manager = PluginManager()
        manager.descriptors = {
            'dependency': PluginDescriptor(
                plugin_id='dependency',
                name='Dependency',
                version='1.0.0',
                description='',
                entrypoint='dependency:plugin',
                source='test',
            ),
            'dependent': PluginDescriptor(
                plugin_id='dependent',
                name='Dependent',
                version='1.0.0',
                description='',
                entrypoint='dependent:plugin',
                source='test',
            ),
        }
        plugins = {
            'dependency:plugin': _DependencyPlugin(),
            'dependent:plugin': _DependentPlugin(),
        }
        manager._load_object = MagicMock(side_effect=plugins.get)
        manager.configure_user(self.user, {'dependency', 'dependent'})
        with_dependency = manager.run_hook('example', 1, None, self.user)

        manager.configure_user(self.user, {'dependent'})
        without_dependency = manager.run_hook('example', 1, None, self.user)

        self.assertEqual(with_dependency, 2)
        self.assertEqual(without_dependency, 1)
        dependent_status = next(
            status
            for status in manager.plugin_statuses(self.user)
            if status['id'] == 'dependent'
        )
        self.assertIn(
            'must be enabled first',
            dependent_status['error'],
        )


class ReferencePluginIntegrationTests(TestCase):
    def setUp(self):
        plugin_manager.discover()
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.client.login(username='testuser', password='testpass')

    def tearDown(self):
        plugin_manager.discover()

    def test_reference_plugins_are_discovered_and_disabled_by_default(self):
        response = self.client.get(reverse('dashboard:settings'))

        self.assertContains(response, 'Pull Request Filters')
        self.assertContains(response, 'GitHub Status')
        self.assertFalse(
            PluginConfiguration.objects.filter(user=self.user, enabled=True).exists()
        )

    def test_settings_enable_only_selected_plugins(self):
        response = self.client.post(
            reverse('dashboard:save_plugins'),
            {'enabled_plugins': ['pull-request-filters']},
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(PluginConfiguration.objects.get(
            user=self.user,
            plugin_id='pull-request-filters',
        ).enabled)
        self.assertFalse(PluginConfiguration.objects.filter(
            user=self.user,
            plugin_id='github-status',
            enabled=True,
        ).exists())

    @patch('dashboard.views.GitHubClient')
    def test_filter_ui_only_appears_when_plugin_is_enabled(self, mock_github_client):
        github_client = MagicMock()
        github_client.get_all_user_prs.return_value = []
        github_client.get_username.return_value = 'testuser'
        github_client.errors = []
        github_client.warnings = []
        mock_github_client.return_value = github_client

        disabled_response = self.client.get(reverse('dashboard:pr_list'))
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='pull-request-filters',
            enabled=True,
        )
        enabled_response = self.client.get(reverse('dashboard:pr_list'))

        self.assertNotContains(disabled_response, 'CI Status')
        self.assertContains(enabled_response, 'CI Status')

    @patch('dashboard.views.GitHubClient')
    def test_filter_hook_changes_fetch_options(self, mock_github_client):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='pull-request-filters',
            enabled=True,
        )
        github_client = MagicMock()
        github_client.get_all_user_prs.return_value = []
        github_client.get_username.return_value = 'testuser'
        github_client.errors = []
        github_client.warnings = []
        mock_github_client.return_value = github_client

        self.client.get(reverse('dashboard:pr_list'), {'author': 'octocat'})

        github_client.get_all_user_prs.assert_called_once_with([], author='octocat')

    def test_github_status_route_is_unavailable_until_enabled(self):
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'github-status', 'route': 'status'},
        )

        disabled_response = self.client.get(url)
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='github-status',
            enabled=True,
        )
        with patch(
            'prdash_github_status.plugin.get_github_status',
            return_value=SimpleNamespace(known=False),
        ):
            enabled_response = self.client.get(url)

        self.assertEqual(disabled_response.status_code, 404)
        self.assertEqual(enabled_response.status_code, 200)

    def test_pull_request_query_contract_is_mutable_by_hook(self):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='pull-request-filters',
            enabled=True,
        )
        request = RequestFactory().get('/prs/', {'draft': 'ready'})
        request.user = self.user
        context = PullRequestListContext(
            request=request,
            client=MagicMock(),
            active_tab='open',
            current_username='testuser',
        )

        query = plugin_manager.run_hook(
            'pr_list.query',
            PullRequestQuery(),
            context,
            self.user,
            request,
        )

        self.assertEqual(query.parameters['draft'], 'ready')
        self.assertTrue(query.affects_count)
