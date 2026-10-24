import json
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.http import Http404
from django.template import Context
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from dashboard.models import (
    PersonalAccessToken,
    PluginConfiguration,
    PluginUserData,
    TrackedRepository,
)
from dashboard.github_client import CIStatus, PullRequestInfo, ReviewStatus
from dashboard.plugin_manager import PluginDescriptor, PluginManager, plugin_manager
from prdash.plugin_worker import _Registration, _WorkerRegistrar
from prdash.plugin_api import (
    PLUGIN_API_VERSION,
    RequestInfo,
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

    def test_source_plugin_assets_resolve_inside_package_static_directory(self):
        with TemporaryDirectory() as directory:
            plugin_directory = Path(directory) / 'asset-plugin'
            asset = plugin_directory / 'src' / 'asset_plugin' / 'static' / 'app.js'
            asset.parent.mkdir(parents=True)
            asset.write_text('window.assetPlugin = true;', encoding='utf-8')
            manifest = {
                'id': 'asset-plugin',
                'name': 'Asset Plugin',
                'version': '1.0.0',
                'api_version': PLUGIN_API_VERSION,
                'entrypoint': 'asset_plugin.plugin:plugin',
                'python_path': 'src',
            }
            (plugin_directory / 'prdash-plugin.json').write_text(
                json.dumps(manifest),
                encoding='utf-8',
            )

            with override_settings(PRDASH_PLUGIN_PATHS=[directory]):
                manager = PluginManager()
                manager.discover()

            self.assertEqual(manager.asset_path('asset-plugin', 'app.js'), asset.resolve())
            with self.assertRaises(Http404):
                manager.asset_path('asset-plugin', '../app.js')
            with self.assertRaises(Http404):
                manager.asset_path('asset-plugin', 'style.css')

    def test_incompatible_source_is_discovered_but_not_loaded(self):
        manager = PluginManager()
        manager.descriptors['example'] = PluginDescriptor(
            plugin_id='example',
            name='Example',
            version='1.0.0',
            description='',
            entrypoint='unused:plugin',
            source='test',
            api_version='1.0',
        )

        loaded = manager.load('example', {'example'})

        self.assertIsNone(loaded)
        self.assertIn('Requires plugin API 1.0', manager.descriptors['example'].load_error)


class PluginRuntimeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.factory = RequestFactory()

    def manager_with_plugin(self, permissions=()):
        tempdir = TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        plugin_dir = root / 'hook-plugin'
        plugin_dir.mkdir()
        shutdown_marker = plugin_dir / 'shutdown.txt'
        module_source = f'''\
from pathlib import Path
from prdash.plugin_api import PLUGIN_API_VERSION, PluginJsonResponse, PluginMetadata

class HookPlugin:
    metadata = PluginMetadata(
        plugin_id='hook-plugin', name='Hook Plugin', version='1.0.0',
        api_version=PLUGIN_API_VERSION, permissions={tuple(permissions)!r},
    )

    def initialize(self, registrar):
        self.shutdown_marker = Path({str(shutdown_marker)!r})
        self.registrar = registrar
        registrar.register_hook('example', self.hook)
        registrar.register_route('hello', self.hello)
        registrar.register_route('inspect_user', self.inspect_user)
        registrar.register_service('value', lambda args: 42)

    def shutdown(self):
        self.shutdown_marker.write_text('stopped', encoding='utf-8')

    @staticmethod
    def hook(value, context, config):
        if value == 'unchanged':
            raise RuntimeError('broken')
        return value + 1 if isinstance(value, int) else value

    @staticmethod
    def hello(request, config):
        return PluginJsonResponse({{'message': 'hello'}})

    def inspect_user(self, request, config):
        user_id = int(request.query_params['user_id'])
        item = self.registrar.get_user_data(user_id, 'private', 'secret')
        return PluginJsonResponse({{
            'config': self.registrar.get_user_config(user_id),
            'data': item.value if item else None,
            'secret': self.registrar.get_user_secret(user_id, 'api_key'),
            'repositories': self.registrar.list_tracked_repositories(user_id),
            'token': self.registrar.resolve_github_token(user_id),
        }})

plugin = HookPlugin()
'''
        (plugin_dir / 'hook_plugin.py').write_text(module_source, encoding='utf-8')
        (plugin_dir / 'prdash-plugin.json').write_text(json.dumps({
            'id': 'hook-plugin',
            'name': 'Hook Plugin',
            'version': '1.0.0',
            'api_version': PLUGIN_API_VERSION,
            'entrypoint': 'hook_plugin:plugin',
            'permissions': list(permissions),
        }), encoding='utf-8')
        manager = PluginManager()
        with override_settings(PRDASH_PLUGIN_PATHS=[str(root)]):
            manager.discover()
        self.addCleanup(manager._shutdown_all)
        manager.shutdown_marker = shutdown_marker
        return manager

    def test_plugin_only_executes_after_user_enables_it(self):
        manager = self.manager_with_plugin()
        context = PullRequestListContext(request=None, active_tab='open', current_username=None)

        disabled = manager.run_hook('example', 1, context, self.user)
        manager.configure_user(self.user, {'hook-plugin'})
        enabled = manager.run_hook('example', 1, context, self.user)

        self.assertEqual(disabled, 1)
        self.assertEqual(enabled, 2)

    def test_hook_failure_is_isolated(self):
        manager = self.manager_with_plugin()
        manager.configure_user(self.user, {'hook-plugin'})
        context = PullRequestListContext(request=None, active_tab='open', current_username=None)

        result = manager.run_hook('example', 'unchanged', context, self.user)

        self.assertEqual(result, 'unchanged')

    def test_disabling_last_user_shuts_plugin_down(self):
        manager = self.manager_with_plugin()
        manager.configure_user(self.user, {'hook-plugin'})

        manager.configure_user(self.user, set())

        self.assertTrue(manager.shutdown_marker.exists())
        self.assertNotIn('hook-plugin', manager._workers)

    def test_enabled_plugin_route_and_service_are_available(self):
        manager = self.manager_with_plugin()
        manager.configure_user(self.user, {'hook-plugin'})
        request = self.factory.get('/plugins/hook-plugin/hello/')
        request.user = self.user

        response = manager.dispatch(request, 'hook-plugin', 'hello')

        self.assertEqual(json.loads(response.content), {'message': 'hello'})
        self.assertEqual(
            manager.get_service(self.user, 'hook-plugin', 'value'),
            42,
        )

    def test_route_callbacks_are_bound_to_the_request_user(self):
        permissions = ('github.token', 'repos.read')
        manager = self.manager_with_plugin(permissions)
        other_user = User.objects.create_user(username='other', password='testpass')
        manager.configure_user(self.user, {'hook-plugin'}, {'hook-plugin': permissions})
        manager.configure_user(other_user, {'hook-plugin'}, {'hook-plugin': permissions})

        manager.update_user_config(self.user, 'hook-plugin', {'owner': 'first'})
        manager.update_user_config(other_user, 'hook-plugin', {'owner': 'other'})
        manager.set_user_data(self.user, 'hook-plugin', 'private', 'secret', {'owner': 'first'})
        manager.set_user_data(other_user, 'hook-plugin', 'private', 'secret', {'owner': 'other'})
        manager.set_user_secret(self.user, 'hook-plugin', 'api_key', 'first-key')
        manager.set_user_secret(other_user, 'hook-plugin', 'api_key', 'other-key')
        TrackedRepository.objects.create(user=self.user, owner='first', name='repo')
        TrackedRepository.objects.create(user=other_user, owner='other', name='repo')
        PersonalAccessToken.objects.create(user=self.user, token='first-token')
        PersonalAccessToken.objects.create(user=other_user, token='other-token')

        first_request = self.factory.get(
            '/plugins/hook-plugin/inspect_user/', {'user_id': other_user.id}
        )
        first_request.user = self.user
        first_response = manager.dispatch(first_request, 'hook-plugin', 'inspect_user')

        other_request = self.factory.get(
            '/plugins/hook-plugin/inspect_user/', {'user_id': self.user.id}
        )
        other_request.user = other_user
        other_response = manager.dispatch(other_request, 'hook-plugin', 'inspect_user')

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(json.loads(first_response.content), {
            'config': {'owner': 'first'},
            'data': {'owner': 'first'},
            'secret': 'first-key',
            'repositories': [{'owner': 'first', 'name': 'repo', 'enabled': True}],
            'token': 'first-token',
        })
        self.assertEqual(other_response.status_code, 200)
        self.assertEqual(json.loads(other_response.content), {
            'config': {'owner': 'other'},
            'data': {'owner': 'other'},
            'secret': 'other-key',
            'repositories': [{'owner': 'other', 'name': 'repo', 'enabled': True}],
            'token': 'other-token',
        })

    def test_callbacks_require_a_declared_and_approved_permission(self):
        undeclared = self.manager_with_plugin()
        undeclared.configure_user(self.user, {'hook-plugin'})
        request = self.factory.get('/plugins/hook-plugin/inspect_user/', {'user_id': self.user.id})
        request.user = self.user
        self.assertEqual(undeclared.dispatch(request, 'hook-plugin', 'inspect_user').status_code, 500)

    def test_plugin_stays_disabled_until_its_permissions_are_approved(self):
        permissions = ('github.token', 'repos.read')
        manager = self.manager_with_plugin(permissions)
        manager.configure_user(self.user, {'hook-plugin'})
        status = manager.plugin_statuses(self.user)[0]
        self.assertTrue(status['needs_approval'])
        self.assertEqual([item['name'] for item in status['permissions']], list(permissions))
        self.assertEqual(manager._enabled_ids(self.user), set())

        manager.configure_user(self.user, {'hook-plugin'}, {'hook-plugin': ['github.token']})
        self.assertEqual(manager._enabled_ids(self.user), set())

        manager.configure_user(self.user, {'hook-plugin'}, {'hook-plugin': permissions})
        self.assertFalse(manager.plugin_statuses(self.user)[0]['needs_approval'])
        self.assertEqual(manager._enabled_ids(self.user), {'hook-plugin'})

    def test_user_scoped_callbacks_are_denied_without_an_active_user(self):
        connection = MagicMock()
        registrar = _WorkerRegistrar(connection, _Registration(), {})

        with self.assertRaises(PermissionError):
            registrar.get_user_data(self.user.id, 'private', 'secret')
        connection.callback.assert_not_called()

        manager = self.manager_with_plugin()
        worker = SimpleNamespace(plugin_id='hook-plugin')
        with self.assertRaises(PermissionError):
            manager._handle_callback(
                worker,
                'get_user_data',
                {
                    'user_id': self.user.id,
                    'collection': 'private',
                    'key': 'secret',
                },
            )

    def test_plugin_user_data_is_scoped_to_a_user_and_collection(self):
        manager = self.manager_with_plugin()
        other_user = User.objects.create_user(username='other', password='testpass')

        manager.set_user_data(self.user, 'hook-plugin', 'saved_items', 'first', {'value': 1})
        manager.set_user_data(self.user, 'hook-plugin', 'saved_items', 'second', {'value': 2})
        manager.set_user_data(self.user, 'hook-plugin', 'other_items', 'first', {'value': 2})
        manager.reorder_user_data(self.user, 'hook-plugin', 'saved_items', ['second', 'first'])

        saved_items = manager.list_user_data(self.user, 'hook-plugin', 'saved_items')

        self.assertEqual(
            [(item.key, item.value) for item in saved_items],
            [('second', {'value': 2}), ('first', {'value': 1})],
        )
        self.assertIsNone(manager.get_user_data(other_user, 'hook-plugin', 'saved_items', 'first'))
        self.assertTrue(manager.delete_user_data(self.user, 'hook-plugin', 'saved_items', 'first'))
        self.assertFalse(manager.delete_user_data(self.user, 'hook-plugin', 'saved_items', 'first'))

    def test_plugin_user_data_rejects_non_standard_json_values(self):
        manager = self.manager_with_plugin()

        with self.assertRaisesMessage(ValueError, 'JSON serializable'):
            manager.set_user_data(
                self.user,
                'hook-plugin',
                'saved_items',
                'invalid',
                {'value': float('nan')},
            )

    def test_plugin_user_secrets_are_scoped_to_a_user_and_plugin(self):
        manager = self.manager_with_plugin()
        other_user = User.objects.create_user(username='other', password='testpass')

        manager.set_user_secret(self.user, 'hook-plugin', 'api_key', 'first')
        manager.set_user_secret(self.user, 'hook-plugin', 'api_key', 'second')

        self.assertEqual(manager.get_user_secret(self.user, 'hook-plugin', 'api_key'), 'second')
        self.assertIsNone(manager.get_user_secret(other_user, 'hook-plugin', 'api_key'))
        self.assertIsNone(manager.get_user_secret(self.user, 'other-plugin', 'api_key'))
        self.assertTrue(manager.delete_user_secret(self.user, 'hook-plugin', 'api_key'))
        self.assertFalse(manager.delete_user_secret(self.user, 'hook-plugin', 'api_key'))
        self.assertIsNone(manager.get_user_secret(self.user, 'hook-plugin', 'api_key'))

    def test_plugin_user_secrets_reject_invalid_keys_and_values(self):
        manager = self.manager_with_plugin()

        for key in ('', 'Bad Key', 'x' * 65, None):
            with self.assertRaisesMessage(ValueError, 'simple name'):
                manager.set_user_secret(self.user, 'hook-plugin', key, 'value')
        for value in ('', 'x' * 1025, 42):
            with self.assertRaisesMessage(ValueError, 'between 1 and 1024'):
                manager.set_user_secret(self.user, 'hook-plugin', 'api_key', value)

    def test_request_info_survives_form_data_stream_parsing(self):
        request = self.factory.post('/example/', {'value': 'test'})
        request.user = self.user
        form_data = request.POST

        info = PluginManager._build_request_info_dict(request)

        self.assertEqual(info['form_params'], dict(form_data.items()))
        self.assertEqual(info['body'], '')

    def test_ui_context_provider_adds_request_specific_template_context(self):
        tempdir = TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        plugin_dir = root / 'ui-context'
        package_dir = plugin_dir / 'ui_context'
        package_dir.mkdir(parents=True)
        (package_dir / '__init__.py').write_text('', encoding='utf-8')
        template_path = package_dir / 'template.html'
        template_path.write_text('{{ from_provider }}', encoding='utf-8')
        (package_dir / 'plugin.py').write_text('''\
from prdash.plugin_api import (
    PLUGIN_API_VERSION, PluginMetadata, TemplateResource, UIContribution,
)

class UIContextPlugin:
    metadata = PluginMetadata(
        plugin_id='ui-context', name='UI Context', version='1.0.0',
        api_version=PLUGIN_API_VERSION,
    )

    def initialize(self, registrar):
        registrar.register_ui(UIContribution(
            slot='example',
            template=TemplateResource('ui_context', 'template.html'),
            context_provider=lambda request, config: {'from_provider': request.path},
        ))

    def shutdown(self):
        pass

plugin = UIContextPlugin()
''', encoding='utf-8')
        (plugin_dir / 'prdash-plugin.json').write_text(json.dumps({
            'id': 'ui-context',
            'name': 'UI Context',
            'version': '1.0.0',
            'api_version': PLUGIN_API_VERSION,
            'entrypoint': 'ui_context.plugin:plugin',
            'python_path': '.',
        }), encoding='utf-8')
        manager = PluginManager()
        manager.FINGERPRINT_CHECK_INTERVAL = 0
        with override_settings(DEBUG=True, PRDASH_PLUGIN_PATHS=[str(root)]):
            manager.discover()
            self.addCleanup(manager._shutdown_all)
            manager.configure_user(self.user, {'ui-context'})
            request = self.factory.get('/example/')
            request.user = self.user

            context = Context({'request': request})
            result = manager.render_slot('example', context)
            self.assertEqual(result, '/example/')

            template_path.write_text('Updated {{ from_provider }}', encoding='utf-8')
            result = manager.render_slot('example', context)

        self.assertEqual(result, 'Updated /example/')

    def test_dependency_must_remain_enabled_for_plugin_execution(self):
        tempdir = TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        dependency_dir = root / 'dependency'
        dependency_dir.mkdir()
        (dependency_dir / 'dependency_plugin.py').write_text('''\
from prdash.plugin_api import PLUGIN_API_VERSION, PluginMetadata

class DependencyPlugin:
    metadata = PluginMetadata(
        plugin_id='dependency', name='Dependency', version='1.0.0',
        api_version=PLUGIN_API_VERSION,
    )
    def initialize(self, registrar):
        pass
    def shutdown(self):
        pass

plugin = DependencyPlugin()
''', encoding='utf-8')
        dependent_dir = root / 'dependent'
        dependent_dir.mkdir()
        assets_dir = dependent_dir / 'dependent_assets'
        assets_dir.mkdir()
        (assets_dir / '__init__.py').write_text('', encoding='utf-8')
        (assets_dir / 'template.html').write_text('dependent', encoding='utf-8')
        (dependent_dir / 'dependent_plugin.py').write_text('''\
from prdash.plugin_api import (
    PLUGIN_API_VERSION, PluginDependency, PluginMetadata, TemplateResource, UIContribution,
)

class DependentPlugin:
    metadata = PluginMetadata(
        plugin_id='dependent', name='Dependent', version='1.0.0',
        api_version=PLUGIN_API_VERSION,
        dependencies=(PluginDependency('dependency', '>=1,<2'),),
    )
    def initialize(self, registrar):
        registrar.register_hook('example', self.increment)
        registrar.register_ui(UIContribution(
            slot='example', template=TemplateResource('dependent_assets', 'template.html'),
        ))
    def shutdown(self):
        pass
    @staticmethod
    def increment(value, context, config):
        return value + 1

plugin = DependentPlugin()
''', encoding='utf-8')
        for plugin_id, module_name, display_name in (
            ('dependency', 'dependency_plugin', 'Dependency'),
            ('dependent', 'dependent_plugin', 'Dependent'),
        ):
            plugin_dir = root / plugin_id
            (plugin_dir / 'prdash-plugin.json').write_text(json.dumps({
                'id': plugin_id,
                'name': display_name,
                'version': '1.0.0',
                'api_version': PLUGIN_API_VERSION,
                'entrypoint': f'{module_name}:plugin',
                'python_path': '.',
            }), encoding='utf-8')
        manager = PluginManager()
        with override_settings(PRDASH_PLUGIN_PATHS=[str(root)]):
            manager.discover()
        self.addCleanup(manager._shutdown_all)
        request = self.factory.get('/example/')
        request.user = self.user
        manager.configure_user(self.user, {'dependency', 'dependent'})
        context = PullRequestListContext(request=None, active_tab='open', current_username=None)
        with_dependency = manager.run_hook('example', 1, context, self.user)
        with_dependency_slot = manager.render_slot('example', Context({'request': request}))
        with_dependency_statuses = {
            status['id']: status for status in manager.plugin_statuses(self.user)
        }

        manager.configure_user(self.user, {'dependent'})
        without_dependency = manager.run_hook('example', 1, context, self.user)
        blocked_request = self.factory.get('/example/')
        blocked_request.user = self.user
        without_dependency_slot = manager.render_slot(
            'example',
            Context({'request': blocked_request}),
        )

        without_dependency_statuses = {
            status['id']: status for status in manager.plugin_statuses(self.user)
        }

        self.assertEqual(with_dependency, 2)
        self.assertEqual(with_dependency_slot, 'dependent')
        self.assertTrue(with_dependency_statuses['dependent']['loaded'])
        self.assertEqual(without_dependency, 1)
        self.assertEqual(without_dependency_slot, '')
        self.assertIsNone(without_dependency_statuses['dependent']['error'])
        self.assertEqual(
            [(item['id'], item['problem']) for item in without_dependency_statuses['dependent']['unmet_requires']],
            [('dependency', 'disabled')],
        )
        self.assertEqual(with_dependency_statuses['dependent']['unmet_requires'], [])


class ReferencePluginIntegrationTests(TestCase):
    def setUp(self):
        plugin_manager.discover()
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.client.login(username='testuser', password='testpass')

    @staticmethod
    def _plugin(plugin_id, package_name, class_name):
        plugin_path = (
            Path(__file__).resolve().parents[2]
            / 'plugins'
            / plugin_id
            / 'src'
            / package_name
            / 'plugin.py'
        )
        spec = importlib.util.spec_from_file_location(f'test_plugin_{plugin_id}', plugin_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin = getattr(module, class_name)()
        plugin.registrar = SimpleNamespace(resolve_github_token=lambda _user_id: 'token')
        return plugin

    def _request_info(self, method, *, query_params=None, form_params=None, form_lists=None):
        return RequestInfo(
            method=method,
            path='/plugins/test/route/',
            query_params=query_params or {},
            form_params=form_params or {},
            form_lists=form_lists or {},
            user_id=self.user.id,
            username=self.user.username,
            is_authenticated=True,
        )

    def tearDown(self):
        plugin_manager.discover()

    def test_reference_plugins_are_discovered_and_disabled_by_default(self):
        response = self.client.get(reverse('dashboard:settings'))

        self.assertContains(response, 'Pull Request Filters')
        self.assertContains(response, 'GitHub Status')
        self.assertContains(response, 'GitHub Actions Re-run Failed Jobs')
        self.assertContains(response, 'GitHub PR Preview')
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
            granted_permissions=['network'],
        ).exists())
        self.assertContains(response, 'Refresh the page to apply the changes.')
        self.assertContains(response, reverse('dashboard:pr_list'))

    def test_settings_does_not_suggest_refresh_without_changes(self):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='pull-request-filters',
            enabled=True,
        )

        response = self.client.post(
            reverse('dashboard:save_plugins'),
            {'enabled_plugins': ['pull-request-filters']},
            HTTP_HX_REQUEST='true',
        )

        self.assertNotContains(response, 'Refresh the page to apply the changes.')

    @patch('dashboard.views.GitHubClient')
    def test_filter_ui_only_appears_when_plugin_is_enabled(self, mock_github_client):
        github_client = MagicMock()
        github_client.get_all_user_prs.return_value = []
        github_client.get_username.return_value = 'testuser'
        github_client.errors = []
        github_client.warnings = []
        mock_github_client.return_value = github_client

        disabled_response = self.client.get(reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true')
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='pull-request-filters',
            enabled=True,
        )
        enabled_response = self.client.get(reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true')

        self.assertNotContains(disabled_response, 'CI Status')
        self.assertContains(enabled_response, 'CI Status')

    @patch('dashboard.views.GitHubClient')
    def test_rerun_button_appears_beside_failed_ci_count(self, mock_github_client):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='github-actions-rerun-failed-jobs',
            enabled=True,
            granted_permissions=['github.token', 'network'],
        )
        github_client = MagicMock()
        github_client.get_all_user_prs.return_value = [PullRequestInfo(
            number=123,
            title='Failed CI',
            url='https://github.com/owner/repo/pull/123',
            repo_owner='owner',
            repo_name='repo',
            author='testuser',
            author_avatar='',
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            labels=[],
            ci_status=CIStatus(
                state='failure',
                passed_count=1,
                total_count=2,
                failed_workflow_run_ids=(456,),
            ),
            review_status=ReviewStatus(state='not_reviewed'),
            draft=False,
            additions=1,
            deletions=1,
        )]
        github_client.get_username.return_value = 'testuser'
        github_client.errors = []
        github_client.warnings = []
        mock_github_client.return_value = github_client

        response = self.client.get(reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true')

        self.assertContains(response, 'CI: 1/2')
        self.assertContains(response, 'Re-run failed CI jobs')
        self.assertContains(
            response,
            reverse(
                'dashboard:plugin_route',
                kwargs={'plugin_id': 'github-actions-rerun-failed-jobs', 'route': 'rerun'},
            ),
        )

    @patch('dashboard.views.GitHubClient')
    def test_pr_preview_button_appears_for_every_pull_request(self, mock_github_client):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='github-pr-preview',
            enabled=True,
            granted_permissions=['github.token', 'network'],
        )
        github_client = MagicMock()
        github_client.get_all_user_prs.return_value = [PullRequestInfo(
            number=123,
            title='Preview me',
            url='https://github.com/owner/repo/pull/123',
            repo_owner='owner',
            repo_name='repo',
            author='testuser',
            author_avatar='',
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            labels=[],
            ci_status=CIStatus(state='unknown'),
            review_status=ReviewStatus(state='not_reviewed'),
            draft=False,
            additions=1,
            deletions=1,
        )]
        github_client.get_username.return_value = 'testuser'
        github_client.errors = []
        github_client.warnings = []
        mock_github_client.return_value = github_client

        response = self.client.get(reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true')

        self.assertContains(response, 'Review pull request diff')
        self.assertContains(
            response,
            reverse(
                'dashboard:plugin_route',
                kwargs={'plugin_id': 'github-pr-preview', 'route': 'preview'},
            ),
        )

    @patch('dashboard.views.GitHubClient')
    def test_export_links_renders_buttons_with_saved_formats(self, mock_github_client):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='export-links',
            enabled=True,
        )
        github_client = MagicMock()
        github_client.get_all_user_prs.return_value = [PullRequestInfo(
            number=123,
            title='Copy me',
            url='https://github.com/owner/repo/pull/123',
            repo_owner='owner',
            repo_name='repo',
            author='testuser',
            author_avatar='',
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            labels=[],
            ci_status=CIStatus(state='unknown'),
            review_status=ReviewStatus(state='not_reviewed'),
            draft=False,
            additions=1,
            deletions=1,
        )]
        github_client.get_username.return_value = 'testuser'
        github_client.errors = []
        github_client.warnings = []
        mock_github_client.return_value = github_client
        settings_url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'export-links', 'route': 'settings'},
        )

        invalid_response = self.client.post(
            settings_url, {'single_format': '', 'list_format': '{url}', 'separator': 'newline'},
        )
        settings_response = self.client.post(
            settings_url,
            {'single_format': '[{title}]({url})', 'list_format': '- {url}', 'separator': 'comma'},
        )
        list_response = self.client.get(reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true')
        page_response = self.client.get(reverse('dashboard:pr_list'))

        self.assertContains(invalid_response, 'Formats must be 1 to 200 characters.')
        self.assertContains(settings_response, 'Export Links settings saved.')
        self.assertEqual(
            PluginConfiguration.objects.get(user=self.user, plugin_id='export-links').config,
            {'single_format': '[{title}]({url})', 'list_format': '- {url}', 'separator': 'comma'},
        )
        self.assertContains(list_response, 'export-links-all')
        self.assertContains(list_response, 'data-url="https://github.com/owner/repo/pull/123"')
        self.assertContains(
            page_response,
            '{"single": "[{title}]({url})", "list": "- {url}", "separator": ", "}',
        )

    @patch('requests.get')
    def test_pr_preview_loads_files_and_commentable_diff_lines(self, mock_get):
        pull_response = MagicMock(status_code=200)
        pull_response.json.return_value = {
            'title': 'Preview me',
            'html_url': 'https://github.com/owner/repo/pull/123',
            'head': {'sha': 'head-sha'},
        }
        files_response = MagicMock(status_code=200)
        files_response.json.return_value = [{
            'filename': 'dashboard/views.py',
            'status': 'modified',
            'additions': 1,
            'deletions': 1,
            'patch': '@@ -10,2 +10,2 @@\n unchanged\n-old\n+new',
        }]
        comments_response = MagicMock(status_code=200)
        comments_response.json.return_value = [
            {
                'id': 42,
                'path': 'dashboard/views.py',
                'line': 11,
                'side': 'RIGHT',
                'body': 'Existing inline comment.',
                'created_at': '2026-01-01T12:00:00Z',
                'html_url': 'https://github.com/owner/repo/pull/123#discussion_r1',
                'user': {
                    'login': 'reviewer',
                    'avatar_url': 'https://avatars.githubusercontent.com/u/1',
                },
            },
            {
                'id': 43,
                'in_reply_to_id': 42,
                'path': 'dashboard/views.py',
                'line': 11,
                'side': 'RIGHT',
                'body': 'Existing reply.',
                'created_at': '2026-01-01T12:30:00Z',
                'html_url': 'https://github.com/owner/repo/pull/123#discussion_r1',
                'user': {'login': 'author'},
            },
        ]

        def get_response(url, **kwargs):
            if url.endswith('/files'):
                return files_response
            if url.endswith('/comments'):
                return comments_response
            return pull_response

        mock_get.side_effect = get_response
        plugin = self._plugin(
            'github_pr_preview',
            'prdash_github_pr_preview',
            'GitHubPRPreviewPlugin',
        )
        response = plugin.preview(self._request_info(
            'GET', query_params={'owner': 'owner', 'repository': 'repo', 'number': '123'},
        ), {})

        file_data = response.context['files'][0]
        self.assertEqual(file_data['filename'], 'dashboard/views.py')
        self.assertEqual(file_data['lines'][3]['line_number'], 11)
        self.assertEqual(file_data['lines'][3]['side'], 'RIGHT')
        self.assertEqual(file_data['lines'][3]['comments'][0]['body'], 'Existing inline comment.')
        self.assertEqual(
            file_data['lines'][3]['comments'][0]['replies'][0]['body'],
            'Existing reply.',
        )
        self.assertEqual(response.context['commit_id'], 'head-sha')
        self.assertEqual(mock_get.call_count, 3)

    @patch.dict('prdash.plugin_cache._CACHE', clear=True)
    @patch('requests.get')
    def test_pr_preview_revalidates_with_etag(self, mock_get):
        def ok(url, params=None, headers=None, **kwargs):
            response = MagicMock(status_code=200, headers={'ETag': f'"{url}"'})
            response.json.return_value = [] if params else {'head': {'sha': 'head-sha'}}
            return response

        mock_get.side_effect = ok
        plugin = self._plugin(
            'github_pr_preview',
            'prdash_github_pr_preview',
            'GitHubPRPreviewPlugin',
        )
        request = self._request_info(
            'GET', query_params={'owner': 'owner', 'repository': 'repo', 'number': '123'},
        )
        plugin.preview(request, {})

        mock_get.reset_mock()
        mock_get.side_effect = lambda url, **kwargs: MagicMock(status_code=304, headers={})
        response = plugin.preview(request, {})

        self.assertEqual(response.context['commit_id'], 'head-sha')
        self.assertEqual(mock_get.call_count, 3)
        for call in mock_get.call_args_list:
            self.assertEqual(call.kwargs['headers']['If-None-Match'], f'"{call.args[0]}"')

    @patch('requests.post')
    def test_pr_preview_publishes_inline_comment(self, mock_post):
        mock_post.return_value = MagicMock(status_code=201)
        plugin = self._plugin(
            'github_pr_preview',
            'prdash_github_pr_preview',
            'GitHubPRPreviewPlugin',
        )
        response = plugin.comment(self._request_info('POST', form_params={
            'owner': 'owner',
            'repository': 'repo',
            'number': '123',
            'path': 'dashboard/views.py',
            'line': '11',
            'side': 'RIGHT',
            'commit_id': 'head-sha',
            'body': 'Please simplify this.',
        }), {})

        self.assertEqual(
            json.loads(response.headers['HX-Trigger'])['githubPRPreviewToast'],
            {'message': 'Inline comment published.', 'type': 'success'},
        )
        self.assertEqual(
            mock_post.call_args.kwargs['json'],
            {
                'body': 'Please simplify this.',
                'commit_id': 'head-sha',
                'path': 'dashboard/views.py',
                'line': 11,
                'side': 'RIGHT',
            },
        )

    @patch('requests.post')
    def test_pr_preview_replies_to_inline_comment(self, mock_post):
        mock_post.return_value = MagicMock(status_code=201)
        plugin = self._plugin(
            'github_pr_preview',
            'prdash_github_pr_preview',
            'GitHubPRPreviewPlugin',
        )
        response = plugin.reply(self._request_info('POST', form_params={
            'owner': 'owner',
            'repository': 'repo',
            'number': '123',
            'comment_id': '42',
            'body': 'Thanks for the suggestion.',
        }), {})

        self.assertEqual(
            json.loads(response.headers['HX-Trigger'])['githubPRPreviewToast'],
            {'message': 'Reply published.', 'type': 'success'},
        )
        self.assertEqual(
            mock_post.call_args.args[0],
            'https://api.github.com/repos/owner/repo/pulls/123/comments/42/replies',
        )
        self.assertEqual(mock_post.call_args.kwargs['json'], {'body': 'Thanks for the suggestion.'})

    @patch('requests.put')
    def test_pr_preview_updates_branch(self, mock_put):
        mock_put.return_value = MagicMock(status_code=202)
        plugin = self._plugin(
            'github_pr_preview',
            'prdash_github_pr_preview',
            'GitHubPRPreviewPlugin',
        )
        response = plugin.update_branch(self._request_info('POST', form_params={
            'owner': 'owner',
            'repository': 'repo',
            'number': '123',
        }), {})

        self.assertEqual(
            json.loads(response.headers['HX-Trigger'])['githubPRPreviewToast'],
            {'message': 'Branch update started.', 'type': 'success'},
        )
        self.assertEqual(
            mock_put.call_args.args[0],
            'https://api.github.com/repos/owner/repo/pulls/123/update-branch',
        )

    @patch('requests.put')
    def test_pr_preview_merges_pull_request(self, mock_put):
        mock_put.return_value = MagicMock(status_code=200)
        plugin = self._plugin(
            'github_pr_preview',
            'prdash_github_pr_preview',
            'GitHubPRPreviewPlugin',
        )
        response = plugin.merge(self._request_info('POST', form_params={
            'owner': 'owner',
            'repository': 'repo',
            'number': '123',
        }), {})

        self.assertEqual(
            json.loads(response.headers['HX-Trigger'])['githubPRPreviewToast'],
            {'message': 'Pull request merged.', 'type': 'success'},
        )
        self.assertEqual(
            mock_put.call_args.args[0],
            'https://api.github.com/repos/owner/repo/pulls/123/merge',
        )

    @patch('requests.post')
    def test_rerun_plugin_requests_only_failed_jobs(self, mock_post):
        mock_post.return_value.status_code = 201
        plugin = self._plugin(
            'github_actions_rerun_failed_jobs',
            'prdash_github_actions_rerun_failed_jobs',
            'GitHubActionsRerunFailedJobsPlugin',
        )
        response = plugin.rerun(self._request_info(
            'POST',
            form_params={'owner': 'owner', 'repository': 'repo'},
            form_lists={'run_id': ['42', '7', '42']},
        ), {})

        self.assertEqual(
            json.loads(response.headers['HX-Trigger'])['githubActionsRerunFailedJobsToast'],
            {'message': 'Re-run requested for 2 workflow run(s).', 'type': 'success'},
        )
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(
            mock_post.call_args_list[0].args[0],
            'https://api.github.com/repos/owner/repo/actions/runs/7/rerun-failed-jobs',
        )
        self.assertEqual(
            mock_post.call_args_list[1].args[0],
            'https://api.github.com/repos/owner/repo/actions/runs/42/rerun-failed-jobs',
        )

    @patch('requests.post')
    def test_rerun_plugin_reports_github_errors_as_toasts(self, mock_post):
        mock_post.return_value.status_code = 403
        mock_post.return_value.json.return_value = {
            'message': 'OAuth App access is restricted by this organization.'
        }
        plugin = self._plugin(
            'github_actions_rerun_failed_jobs',
            'prdash_github_actions_rerun_failed_jobs',
            'GitHubActionsRerunFailedJobsPlugin',
        )
        response = plugin.rerun(self._request_info(
            'POST',
            form_params={'owner': 'llvm', 'repository': 'llvm-project'},
            form_lists={'run_id': ['42']},
        ), {})

        self.assertEqual(
            json.loads(response.headers['HX-Trigger'])['githubActionsRerunFailedJobsToast'],
            {
                'message': 'OAuth App access is restricted by this organization.',
                'type': 'error',
            },
        )

    @patch('dashboard.views.GitHubClient')
    def test_saved_views_are_plugin_owned(self, mock_github_client):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='saved-views',
            enabled=True,
        )
        github_client = MagicMock()
        github_client.get_all_user_prs.return_value = []
        github_client.get_username.return_value = 'testuser'
        github_client.errors = []
        github_client.warnings = []
        mock_github_client.return_value = github_client
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'saved-views', 'route': 'categories'},
        )
        query = {
            'open': True,
            'include': {'text': 'bug', 'pills': [{'kind': 'label', 'value': 'urgent'}]},
            'exclude': {'text': '', 'pills': []},
        }

        response = self.client.post(
            url,
            data=json.dumps({'name': 'Needs attention', 'query': query}),
            content_type='application/json',
        )
        list_response = self.client.get(reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['category'], {'name': 'Needs attention', 'query': query})
        self.assertTrue(PluginUserData.objects.filter(
            user=self.user,
            plugin_id='saved-views',
            collection='categories',
            key='Needs attention',
            value={'query': query},
        ).exists())
        self.assertContains(list_response, 'saved-views')
        self.assertContains(list_response, 'Needs attention')

    @patch('dashboard.views.GitHubClient')
    def test_saved_views_accept_author_pill(self, mock_github_client):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='saved-views',
            enabled=True,
        )
        github_client = MagicMock()
        github_client.get_all_user_prs.return_value = []
        github_client.get_username.return_value = 'testuser'
        github_client.errors = []
        github_client.warnings = []
        mock_github_client.return_value = github_client
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'saved-views', 'route': 'categories'},
        )
        query = {
            'open': True,
            'include': {'text': '', 'pills': [{'kind': 'author', 'value': 'octocat'}]},
            'exclude': {'text': '', 'pills': []},
        }

        response = self.client.post(
            url,
            data=json.dumps({'name': 'By octocat', 'query': query}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['category'], {'name': 'By octocat', 'query': query})

    def test_saved_search_category_keeps_alternatives(self):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='saved-views',
            enabled=True,
        )
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'saved-views', 'route': 'categories'},
        )
        empty = {'text': '', 'pills': []}
        query = {
            'open': True,
            'include': empty,
            'exclude': empty,
            'alternatives': [{
                'include': {'text': 'bug', 'pills': []},
                'exclude': empty,
            }],
        }

        response = self.client.post(
            url,
            data=json.dumps({'name': 'Either', 'query': query}),
            content_type='application/json',
        )
        invalid = self.client.post(
            url,
            data=json.dumps({'name': 'Bad', 'query': {**query, 'alternatives': ['x']}}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['category']['query'], query)
        self.assertEqual(invalid.status_code, 400)

    def test_saved_search_category_rejects_empty_query(self):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='saved-views',
            enabled=True,
        )
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'saved-views', 'route': 'categories'},
        )

        response = self.client.post(
            url,
            data=json.dumps({
                'name': 'Empty',
                'query': {
                    'include': {'text': '', 'pills': []},
                    'exclude': {'text': '', 'pills': []},
                },
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PluginUserData.objects.filter(
            user=self.user,
            plugin_id='saved-views',
        ).exists())

    def test_saved_search_category_rejects_non_object_payload(self):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='saved-views',
            enabled=True,
        )
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'saved-views', 'route': 'categories'},
        )

        response = self.client.post(url, data='[]', content_type='application/json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'Request body must contain a JSON object')

    def test_saved_views_keep_user_order(self):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='saved-views',
            enabled=True,
        )
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'saved-views', 'route': 'categories'},
        )
        query = {
            'include': {'text': 'bug', 'pills': []},
            'exclude': {'text': '', 'pills': []},
        }
        for name in ('First', 'Second'):
            self.client.post(
                url,
                data=json.dumps({'name': name, 'query': query}),
                content_type='application/json',
            )

        response = self.client.put(
            url,
            data=json.dumps({'names': ['Second', 'First']}),
            content_type='application/json',
        )
        categories = self.client.get(url).json()['categories']

        self.assertEqual(response.status_code, 200)
        self.assertEqual([category['name'] for category in categories], ['Second', 'First'])

    @patch('dashboard.views.GitHubClient')
    def test_filter_plugin_does_not_fetch_by_author(self, mock_github_client):
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

        self.client.get(reverse('dashboard:pr_list'), {'author': 'octocat'}, HTTP_HX_REQUEST='true')

        github_client.get_all_user_prs.assert_called_once_with([])

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
            granted_permissions=['network'],
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


_STREAM_PLUGIN_SOURCE = '''
from prdash.plugin_api import PluginJsonResponse, PluginMetadata, PluginStreamChunk


class StreamPlugin:
    metadata = PluginMetadata(
        plugin_id='stream-plugin',
        name='Stream Plugin',
        version='1.0.0',
        api_version='3.0',
    )

    def initialize(self, registrar):
        registrar.register_route('progress', self.progress)
        registrar.register_route('plain', self.plain)

    def shutdown(self):
        pass

    @staticmethod
    def progress(request, config):
        yield PluginStreamChunk(kind='step', data={'label': 'first'})
        yield PluginStreamChunk(kind='step', data={'label': 'second'})
        return PluginJsonResponse({'done': True})

    @staticmethod
    def plain(request, config):
        return PluginJsonResponse({'ok': True})
'''


class PluginStreamingRuntimeTests(TestCase):
    """Exercises the real worker subprocess, unlike PluginRuntimeTests above (stale _load_object mocking predating
    the subprocess isolation refactor)."""

    def setUp(self):
        self.user = User.objects.create_user(username='streamuser', password='testpass')
        self.factory = RequestFactory()

        tempdir = TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        plugin_dir = root / 'stream-plugin'
        plugin_dir.mkdir()
        (plugin_dir / 'stream_plugin.py').write_text(_STREAM_PLUGIN_SOURCE, encoding='utf-8')
        manifest = {
            'id': 'stream-plugin',
            'name': 'Stream Plugin',
            'version': '1.0.0',
            'api_version': '3.0',
            'entrypoint': 'stream_plugin:StreamPlugin',
        }
        (plugin_dir / 'prdash-plugin.json').write_text(json.dumps(manifest), encoding='utf-8')

        self._override = override_settings(PRDASH_PLUGIN_PATHS=[str(root)])
        self._override.enable()
        self.addCleanup(self._override.disable)

        self.manager = PluginManager()
        self.manager.discover()
        self.addCleanup(self.manager._shutdown_all)
        self.manager.configure_user(self.user, {'stream-plugin'})

    def _request(self):
        request = self.factory.post('/plugins/stream-plugin/progress/')
        request.user = self.user
        return request

    def test_generator_route_streams_chunks_then_final_response(self):
        events = list(self.manager.dispatch_stream(self._request(), 'stream-plugin', 'progress'))

        self.assertEqual(
            [event['chunk'] for event in events if 'chunk' in event],
            [{'kind': 'step', 'data': {'label': 'first'}}, {'kind': 'step', 'data': {'label': 'second'}}],
        )
        final = events[-1]
        self.assertTrue(final.get('final'))
        self.assertEqual(final['response'], {'type': 'json', 'data': {'done': True}, 'status': 200})

    def test_non_generator_route_still_works_via_dispatch(self):
        response = self.manager.dispatch(self._request(), 'stream-plugin', 'plain')

        self.assertEqual(json.loads(response.content), {'ok': True})

    def test_non_generator_route_also_works_via_dispatch_stream(self):
        events = list(self.manager.dispatch_stream(self._request(), 'stream-plugin', 'plain'))

        self.assertEqual(events, [{'final': True, 'response': {'type': 'json', 'data': {'ok': True}, 'status': 200}}])


_PAGE_HELPERS_SOURCE = '''
def rank_of(index):
    return index + 1
'''

_HELPERS_PLUGIN_SOURCE = '''
from prdash.plugin_api import PLUGIN_API_VERSION, PluginMetadata


class HelpersPlugin:
    metadata = PluginMetadata(
        plugin_id='page-helpers', name='Page Helpers', version='1.0.0', api_version=PLUGIN_API_VERSION,
    )

    def initialize(self, registrar):
        pass

    def shutdown(self):
        pass


plugin = HelpersPlugin()
'''

_PAGE_PLUGIN_SOURCE = '''
from page_helpers import rank_of

from prdash.plugin_api import (
    PLUGIN_API_VERSION, PR_LIST_PROCESS_HOOK, PluginMetadata, PluginPrListPage, TemplateResource,
    UIContribution,
)


class PagePlugin:
    metadata = PluginMetadata(
        plugin_id='page-plugin', name='Page Plugin', version='1.0.0', api_version=PLUGIN_API_VERSION,
    )

    def initialize(self, registrar):
        registrar.register_pr_list_page(PluginPrListPage(name='triage', title='Triage'))
        registrar.register_hook(PR_LIST_PROCESS_HOOK, self.process)
        registrar.register_ui(UIContribution(
            slot='example', template=TemplateResource('page_assets', 'badge.html'),
        ))

    def shutdown(self):
        pass

    @staticmethod
    def process(prs, context, config):
        prs = list(reversed(prs))
        for index, pr in enumerate(prs):
            pr['plugin_annotations']['page-plugin'] = {'rank': rank_of(index)}
            pr['plugin_annotations']['other-plugin'] = 'forged'
        return prs
'''


class PluginPrListPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='pageuser', password='testpass')
        tempdir = TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        helpers_dir = root / 'page-helpers'
        helpers_dir.mkdir()
        (helpers_dir / 'page_helpers.py').write_text(_PAGE_HELPERS_SOURCE, encoding='utf-8')
        (helpers_dir / 'helpers_plugin.py').write_text(_HELPERS_PLUGIN_SOURCE, encoding='utf-8')
        plugin_dir = root / 'page-plugin'
        assets_dir = plugin_dir / 'page_assets'
        assets_dir.mkdir(parents=True)
        (assets_dir / '__init__.py').write_text('', encoding='utf-8')
        (assets_dir / 'badge.html').write_text('rank {{ plugin_annotation.rank }}', encoding='utf-8')
        (plugin_dir / 'page_plugin.py').write_text(_PAGE_PLUGIN_SOURCE + '\nplugin = PagePlugin()\n',
                                                   encoding='utf-8')
        for plugin_id, module_name, dependencies in (
            ('page-helpers', 'helpers_plugin', []),
            ('page-plugin', 'page_plugin', ['page-helpers']),
        ):
            (root / plugin_id / 'prdash-plugin.json').write_text(json.dumps({
                'id': plugin_id,
                'name': plugin_id,
                'version': '1.0.0',
                'api_version': PLUGIN_API_VERSION,
                'entrypoint': f'{module_name}:plugin',
                'python_path': '.',
                'dependencies': dependencies,
            }), encoding='utf-8')
        self.manager = PluginManager()
        with override_settings(PRDASH_PLUGIN_PATHS=[str(root)]):
            self.manager.discover()
        self.addCleanup(self.manager._shutdown_all)

    @staticmethod
    def make_pr(number, annotations=None):
        return PullRequestInfo(
            number=number, title=f'PR {number}', url='', repo_owner='org', repo_name='repo',
            author='someone', author_avatar='',
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            labels=[], ci_status=CIStatus(state='success'),
            review_status=ReviewStatus(
                state='commented',
                last_reviewed_at={'pageuser': datetime(2026, 1, 1, 12, tzinfo=timezone.utc)},
            ),
            draft=False, additions=1, deletions=1,
            plugin_annotations=annotations or {},
        )

    def test_pages_are_listed_only_for_users_with_the_plugin_enabled(self):
        self.assertEqual(self.manager.pr_list_pages(self.user), [])

        self.manager.configure_user(self.user, {'page-helpers', 'page-plugin'})

        page = {
            'plugin_id': 'page-plugin', 'name': 'triage', 'title': 'Triage', 'source': 'open_all',
            'fetch_on_demand': False, 'tab': 'plugin:page-plugin:triage',
        }
        self.assertEqual(self.manager.pr_list_pages(self.user), [page])
        self.assertEqual(self.manager.pr_list_page(self.user, 'page-plugin', 'triage'), page)
        self.assertIsNone(self.manager.pr_list_page(self.user, 'page-plugin', 'missing'))

    def test_process_hook_annotations_are_scoped_to_the_calling_plugin(self):
        self.manager.configure_user(self.user, {'page-helpers', 'page-plugin'})
        context = PullRequestListContext(request=None, active_tab='plugin:page-plugin:triage', current_username=None)
        prs = [self.make_pr(1, {'other-plugin': 'kept'}), self.make_pr(2)]

        result = self.manager.run_hook('pr_list.process', prs, context, self.user)

        self.assertEqual([pr.number for pr in result], [2, 1])
        self.assertEqual(result[0].plugin_annotations, {'page-plugin': {'rank': 1}})
        self.assertEqual(result[1].plugin_annotations, {'other-plugin': 'kept', 'page-plugin': {'rank': 2}})
        self.assertEqual(
            result[1].review_status.last_reviewed_at,
            {'pageuser': datetime(2026, 1, 1, 12, tzinfo=timezone.utc)},
        )

    def test_card_slots_receive_the_plugin_annotation(self):
        self.manager.configure_user(self.user, {'page-helpers', 'page-plugin'})
        request = RequestFactory().get('/prs/')
        request.user = self.user

        rendered = self.manager.render_slot(
            'example', Context({'request': request}), pr=self.make_pr(1, {'page-plugin': {'rank': 3}}),
        )

        self.assertEqual(rendered, 'rank 3')

    def test_invalid_pages_are_rejected_at_registration(self):
        from prdash.plugin_api import PluginPrListPage

        registrar = _WorkerRegistrar(MagicMock(), _Registration(), {})
        registrar.register_pr_list_page(PluginPrListPage(name='triage', title='Triage'))

        for page in (
            PluginPrListPage(name='Bad Name', title='Bad'),
            PluginPrListPage(name='untitled', title=''),
            PluginPrListPage(name='other', title='Other', source='closed'),
            PluginPrListPage(name='triage', title='Again'),
        ):
            with self.assertRaises(ValueError):
                registrar.register_pr_list_page(page)

    def test_manifest_dependencies_must_be_plugin_ids(self):
        with self.assertRaises(ValueError):
            PluginManager._manifest_dependency_ids({'dependencies': 'page-helpers'})
        self.assertEqual(PluginManager._manifest_dependency_ids({}), ())
