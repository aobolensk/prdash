from importlib import import_module
from unittest.mock import MagicMock, patch

import requests
from django.test import TestCase

from dashboard.plugin_manager import plugin_manager

plugin_manager.load('github-status', {'github-status'})

status_module = import_module('prdash_github_status.status')
GitHubStatus = status_module.GitHubStatus
ComponentStatus = status_module.ComponentStatus
get_github_status = status_module.get_github_status


class GetGitHubStatusTests(TestCase):
    """Tests for the githubstatus.com component health check."""

    def _components_response(self, components):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'components': components}
        return response

    @patch('prdash_github_status.status.cache')
    @patch('prdash_github_status.status.requests.get')
    def test_all_tracked_components_operational(self, mock_get, mock_cache):
        mock_cache.get.return_value = None
        mock_get.return_value = self._components_response([
            {'name': 'API Requests', 'status': 'operational'},
            {'name': 'Pull Requests', 'status': 'operational'},
            {'name': 'Actions', 'status': 'major_outage'},
        ])

        status = get_github_status()

        self.assertTrue(status.healthy)
        self.assertEqual(status.degraded_components, [])
        self.assertTrue(status.known)

    @patch('prdash_github_status.status.cache')
    @patch('prdash_github_status.status.requests.get')
    def test_tracked_component_outage(self, mock_get, mock_cache):
        mock_cache.get.return_value = None
        mock_get.return_value = self._components_response([
            {'name': 'API Requests', 'status': 'operational'},
            {'name': 'Pull Requests', 'status': 'partial_outage'},
        ])

        status = get_github_status()

        self.assertFalse(status.healthy)
        self.assertTrue(status.outage)
        self.assertEqual(status.warning_components, [])
        self.assertEqual(len(status.outage_components), 1)
        self.assertEqual(status.outage_components[0].name, 'Pull Requests')
        self.assertEqual(status.outage_components[0].status, 'partial_outage')

    @patch('prdash_github_status.status.cache')
    @patch('prdash_github_status.status.requests.get')
    def test_tracked_component_degraded_performance_is_warning_not_outage(self, mock_get, mock_cache):
        mock_cache.get.return_value = None
        mock_get.return_value = self._components_response([
            {'name': 'API Requests', 'status': 'degraded_performance'},
            {'name': 'Pull Requests', 'status': 'operational'},
        ])

        status = get_github_status()

        self.assertFalse(status.healthy)
        self.assertFalse(status.outage)
        self.assertEqual(status.outage_components, [])
        self.assertEqual(len(status.warning_components), 1)
        self.assertEqual(status.warning_components[0].name, 'API Requests')

    @patch('prdash_github_status.status.cache')
    @patch('prdash_github_status.status.requests.get')
    def test_request_failure_returns_unknown(self, mock_get, mock_cache):
        mock_cache.get.return_value = None
        mock_cache.add.return_value = True
        mock_get.side_effect = requests.exceptions.ConnectionError('boom')

        status = get_github_status()

        self.assertFalse(status.known)
        self.assertTrue(status.healthy)

    @patch('prdash_github_status.status.cache')
    @patch('prdash_github_status.status.requests.get')
    def test_malformed_response_returns_unknown(self, mock_get, mock_cache):
        mock_cache.get.return_value = None
        mock_cache.add.return_value = True
        mock_get.return_value = self._components_response(None)

        status = get_github_status()

        self.assertFalse(status.known)

    @patch('prdash_github_status.status.cache')
    def test_uses_cache_when_present(self, mock_cache):
        cached_status = GitHubStatus(
            outage_components=[ComponentStatus('API Requests', 'major_outage')],
        )
        mock_cache.get.return_value = cached_status

        status = get_github_status()

        self.assertIs(status, cached_status)

    @patch('prdash_github_status.status.cache')
    @patch('prdash_github_status.status.requests.get')
    def test_concurrent_miss_does_not_fetch(self, mock_get, mock_cache):
        mock_cache.get.return_value = None
        mock_cache.add.return_value = False

        status = get_github_status()

        mock_get.assert_not_called()
        self.assertFalse(status.known)
