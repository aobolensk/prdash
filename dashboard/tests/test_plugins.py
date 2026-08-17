import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.template import Context
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from dashboard.models import PluginConfiguration, PluginUserData
from dashboard.github_client import CIStatus, PullRequestInfo, ReviewStatus
from dashboard.plugin_manager import PluginDescriptor, PluginManager, plugin_manager
from prdash.plugin_api import (
    PLUGIN_API_VERSION,
    PluginDependency,
    PluginMetadata,
    PullRequestListContext,
    PullRequestQuery,
    TemplateResource,
    UIContribution,
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


class _UiContextPlugin:
    metadata = PluginMetadata(
        plugin_id='ui-context',
        name='UI Context',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
    )

    def initialize(self, registrar):
        registrar.register_ui(UIContribution(
            slot='example',
            template=TemplateResource('unused', 'unused.html'),
            context_provider=lambda request, config: {'from_provider': request.path},
        ))

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
        registrar.register_ui(UIContribution(
            slot='example',
            template=TemplateResource('unused', 'unused.html'),
        ))

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

    def test_plugin_user_data_is_scoped_to_a_user_and_collection(self):
        manager = self.manager_with_plugin(_HookPlugin(lambda value, context, config: value))
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
        manager = self.manager_with_plugin(_HookPlugin(lambda value, context, config: value))

        with self.assertRaisesMessage(ValueError, 'JSON serializable'):
            manager.set_user_data(
                self.user,
                'hook-plugin',
                'saved_items',
                'invalid',
                {'value': float('nan')},
            )

    def test_ui_context_provider_adds_request_specific_template_context(self):
        plugin = _UiContextPlugin()
        manager = PluginManager()
        manager.descriptors['ui-context'] = PluginDescriptor(
            plugin_id='ui-context',
            name='UI Context',
            version='1.0.0',
            description='',
            entrypoint='unused:plugin',
            source='test',
        )
        manager._load_object = MagicMock(return_value=plugin)
        manager._render_resource = MagicMock(return_value='rendered')
        manager.configure_user(self.user, {'ui-context'})
        request = self.factory.get('/example/')
        request.user = self.user

        result = manager.render_slot('example', Context({'request': request}))

        self.assertEqual(result, 'rendered')
        rendered_context = manager._render_resource.call_args.args[1]
        self.assertEqual(rendered_context['from_provider'], '/example/')

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
        manager._render_resource = MagicMock(return_value='rendered')
        request = self.factory.get('/example/')
        request.user = self.user
        manager.configure_user(self.user, {'dependency', 'dependent'})
        with_dependency = manager.run_hook('example', 1, None, self.user)
        with_dependency_slot = manager.render_slot('example', Context({'request': request}))
        with_dependency_statuses = {
            status['id']: status for status in manager.plugin_statuses(self.user)
        }

        manager.configure_user(self.user, {'dependent'})
        without_dependency = manager.run_hook('example', 1, None, self.user)
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
        self.assertEqual(with_dependency_slot, 'rendered')
        self.assertTrue(with_dependency_statuses['dependent']['loaded'])
        self.assertEqual(without_dependency, 1)
        self.assertEqual(without_dependency_slot, '')
        self.assertIn(
            'must be enabled first',
            without_dependency_statuses['dependent']['error'],
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
    def test_rerun_button_appears_beside_failed_ci_count(self, mock_github_client):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='github-actions-rerun-failed-jobs',
            enabled=True,
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

        response = self.client.get(reverse('dashboard:pr_list'))

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

        response = self.client.get(reverse('dashboard:pr_list'))

        self.assertContains(response, 'Review pull request diff')
        self.assertContains(
            response,
            reverse(
                'dashboard:plugin_route',
                kwargs={'plugin_id': 'github-pr-preview', 'route': 'preview'},
            ),
        )

    @patch('requests.get')
    @patch('dashboard.github_client.GitHubClient._get_token', return_value='token')
    def test_pr_preview_loads_files_and_commentable_diff_lines(self, mock_token, mock_get):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='github-pr-preview',
            enabled=True,
        )
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
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'github-pr-preview', 'route': 'preview'},
        )

        response = self.client.get(url, {
            'owner': 'owner', 'repository': 'repo', 'number': '123',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'dashboard/views.py')
        self.assertContains(response, 'github-pr-preview-open-github')
        self.assertContains(response, 'name="line" value="11"')
        self.assertContains(response, 'name="side" value="RIGHT"')
        self.assertContains(response, 'name="commit_id" value="head-sha"')
        self.assertContains(response, 'Existing inline comment.')
        self.assertContains(response, 'Existing reply.')
        self.assertContains(response, 'reviewer')
        self.assertContains(response, 'https://avatars.githubusercontent.com/u/1')
        self.assertContains(response, 'datetime="2026-01-01T12:00:00Z"')
        self.assertContains(response, 'name="comment_id" value="42"')
        content = response.content.decode()
        self.assertLess(content.index('Existing reply.'), content.index('github-pr-preview-reply-button'))

    @patch('requests.post')
    @patch('dashboard.github_client.GitHubClient._get_token', return_value='token')
    def test_pr_preview_publishes_inline_comment(self, mock_token, mock_post):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='github-pr-preview',
            enabled=True,
        )
        mock_post.return_value = MagicMock(status_code=201)
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'github-pr-preview', 'route': 'comment'},
        )

        response = self.client.post(url, {
            'owner': 'owner',
            'repository': 'repo',
            'number': '123',
            'path': 'dashboard/views.py',
            'line': '11',
            'side': 'RIGHT',
            'commit_id': 'head-sha',
            'body': 'Please simplify this.',
        })

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            json.loads(response['HX-Trigger'])['githubPRPreviewToast'],
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
    @patch('dashboard.github_client.GitHubClient._get_token', return_value='token')
    def test_pr_preview_replies_to_inline_comment(self, mock_token, mock_post):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='github-pr-preview',
            enabled=True,
        )
        mock_post.return_value = MagicMock(status_code=201)
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'github-pr-preview', 'route': 'reply'},
        )

        response = self.client.post(url, {
            'owner': 'owner',
            'repository': 'repo',
            'number': '123',
            'comment_id': '42',
            'body': 'Thanks for the suggestion.',
        })

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            json.loads(response['HX-Trigger'])['githubPRPreviewToast'],
            {'message': 'Reply published.', 'type': 'success'},
        )
        self.assertEqual(
            mock_post.call_args.args[0],
            'https://api.github.com/repos/owner/repo/pulls/123/comments/42/replies',
        )
        self.assertEqual(mock_post.call_args.kwargs['json'], {'body': 'Thanks for the suggestion.'})

    @patch('requests.put')
    @patch('dashboard.github_client.GitHubClient._get_token', return_value='token')
    def test_pr_preview_updates_branch(self, mock_token, mock_put):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='github-pr-preview',
            enabled=True,
        )
        mock_put.return_value = MagicMock(status_code=202)
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'github-pr-preview', 'route': 'update-branch'},
        )

        response = self.client.post(url, {
            'owner': 'owner',
            'repository': 'repo',
            'number': '123',
        })

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            json.loads(response['HX-Trigger'])['githubPRPreviewToast'],
            {'message': 'Branch update started.', 'type': 'success'},
        )
        self.assertEqual(
            mock_put.call_args.args[0],
            'https://api.github.com/repos/owner/repo/pulls/123/update-branch',
        )

    @patch('requests.put')
    @patch('dashboard.github_client.GitHubClient._get_token', return_value='token')
    def test_pr_preview_merges_pull_request(self, mock_token, mock_put):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='github-pr-preview',
            enabled=True,
        )
        mock_put.return_value = MagicMock(status_code=200)
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'github-pr-preview', 'route': 'merge'},
        )

        response = self.client.post(url, {
            'owner': 'owner',
            'repository': 'repo',
            'number': '123',
        })

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            json.loads(response['HX-Trigger'])['githubPRPreviewToast'],
            {'message': 'Pull request merged.', 'type': 'success'},
        )
        self.assertEqual(
            mock_put.call_args.args[0],
            'https://api.github.com/repos/owner/repo/pulls/123/merge',
        )

    @patch('requests.post')
    @patch('dashboard.github_client.GitHubClient._get_token', return_value='token')
    def test_rerun_plugin_requests_only_failed_jobs(self, mock_token, mock_post):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='github-actions-rerun-failed-jobs',
            enabled=True,
        )
        mock_post.return_value.status_code = 201
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'github-actions-rerun-failed-jobs', 'route': 'rerun'},
        )

        response = self.client.post(url, {
            'owner': 'owner',
            'repository': 'repo',
            'run_id': ['42', '7', '42'],
        })

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            json.loads(response['HX-Trigger'])['githubActionsRerunFailedJobsToast'],
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
    @patch('dashboard.github_client.GitHubClient._get_token', return_value='token')
    def test_rerun_plugin_reports_github_errors_as_toasts(self, mock_token, mock_post):
        PluginConfiguration.objects.create(
            user=self.user,
            plugin_id='github-actions-rerun-failed-jobs',
            enabled=True,
        )
        mock_post.return_value.status_code = 403
        mock_post.return_value.json.return_value = {
            'message': 'OAuth App access is restricted by this organization.'
        }
        url = reverse(
            'dashboard:plugin_route',
            kwargs={'plugin_id': 'github-actions-rerun-failed-jobs', 'route': 'rerun'},
        )

        response = self.client.post(url, {
            'owner': 'llvm',
            'repository': 'llvm-project',
            'run_id': '42',
        })

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            json.loads(response['HX-Trigger'])['githubActionsRerunFailedJobsToast'],
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
        list_response = self.client.get(reverse('dashboard:pr_list'))

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

        self.client.get(reverse('dashboard:pr_list'), {'author': 'octocat'})

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
