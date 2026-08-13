import time
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth.models import User
from django.test import TestCase

from dashboard.github_client import GRAPHQL_PR_BATCH_SIZE, GitHubClient
from dashboard.models import PersonalAccessToken


class GitHubClientCIStatusTests(TestCase):
    """Tests for GitHub CI status parsing."""

    def _client(self):
        return GitHubClient(user=None)

    def _pr_data(self, rollup_state, contexts):
        return {
            'number': 123,
            'commits': {
                'nodes': [
                    {
                        'commit': {
                            'statusCheckRollup': {
                                'state': rollup_state,
                                'contexts': contexts,
                            }
                        }
                    }
                ]
            },
        }

    def test_rollup_failure_overrides_truncated_green_context_page(self):
        """GitHub rollup state is authoritative for a truncated contexts page."""
        contexts = {
            'totalCount': 174,
            'nodes': (
                [{'conclusion': 'SUCCESS', 'status': 'COMPLETED'} for _ in range(98)]
                + [{'conclusion': 'SKIPPED', 'status': 'COMPLETED'} for _ in range(2)]
            ),
        }

        ci_status = self._client()._parse_ci_status_from_graphql(
            self._pr_data('FAILURE', contexts)
        )

        self.assertEqual(ci_status.state, 'failure')
        self.assertEqual(ci_status.passed_count, 98)
        self.assertEqual(ci_status.total_count, 174)

    def _check_run(self, conclusion, workflow_name, run_number, database_id=None, status='COMPLETED'):
        workflow_run = {
            'runNumber': run_number,
            'workflow': {'name': workflow_name},
        }
        if database_id is not None:
            workflow_run['databaseId'] = database_id
        return {
            'conclusion': conclusion,
            'status': status,
            'checkSuite': {
                'workflowRun': workflow_run,
            },
        }

    def test_superseded_workflow_run_is_ignored(self):
        """A failed run superseded by a passing re-run should not fail the PR."""
        contexts = {
            'totalCount': 4,
            'nodes': [
                # Old run of "CI" (run #1) failed and was cancelled...
                self._check_run('FAILURE', 'CI', 1),
                self._check_run('CANCELLED', 'CI', 1),
                # ...but the latest run (#2) of the same workflow passed.
                self._check_run('SUCCESS', 'CI', 2),
                self._check_run('SUCCESS', 'CI', 2),
            ],
        }

        ci_status = self._client()._parse_ci_status_from_graphql(
            # rollup.state is FAILURE because it aggregates the stale run
            self._pr_data('FAILURE', contexts)
        )

        self.assertEqual(ci_status.state, 'success')
        self.assertEqual(ci_status.passed_count, 2)
        self.assertEqual(ci_status.total_count, 2)

    def test_latest_workflow_run_failure_is_reported(self):
        """A genuine failure in the latest run must still be reported."""
        contexts = {
            'totalCount': 3,
            'nodes': [
                self._check_run('SUCCESS', 'CI', 1),
                self._check_run('SUCCESS', 'CI', 2),
                self._check_run('FAILURE', 'CI', 2),
            ],
        }

        ci_status = self._client()._parse_ci_status_from_graphql(
            self._pr_data('FAILURE', contexts)
        )

        self.assertEqual(ci_status.state, 'failure')

    def test_latest_failed_workflow_run_id_is_exposed(self):
        contexts = {
            'totalCount': 3,
            'nodes': [
                self._check_run('FAILURE', 'CI', 1, database_id=12),
                self._check_run('SUCCESS', 'CI', 2, database_id=13),
                self._check_run('TIMED_OUT', 'CI', 2, database_id=13),
            ],
        }

        ci_status = self._client()._parse_ci_status_from_graphql(
            self._pr_data('FAILURE', contexts)
        )

        self.assertEqual(ci_status.failed_workflow_run_ids, (13,))

    def test_action_required_conclusion_is_a_failure(self):
        """A latest-run check with a failing-but-uncommon conclusion fails the PR."""
        contexts = {
            'totalCount': 2,
            'nodes': [
                self._check_run('SUCCESS', 'CI', 1),
                self._check_run('ACTION_REQUIRED', 'CI', 1),
            ],
        }

        ci_status = self._client()._parse_ci_status_from_graphql(
            self._pr_data('FAILURE', contexts)
        )

        self.assertEqual(ci_status.state, 'failure')

    def test_rollup_success_is_preserved(self):
        contexts = {
            'totalCount': 2,
            'nodes': [
                {'conclusion': 'SUCCESS', 'status': 'COMPLETED'},
                {'state': 'SUCCESS'},
            ],
        }

        ci_status = self._client()._parse_ci_status_from_graphql(
            self._pr_data('SUCCESS', contexts)
        )

        self.assertEqual(ci_status.state, 'success')
        self.assertEqual(ci_status.passed_count, 2)
        self.assertEqual(ci_status.total_count, 2)


class GitHubClientTokenTests(TestCase):
    """Tests for GitHubClient token retrieval."""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')

    def test_get_token_prefers_pat(self):
        """Verify PAT takes priority over OAuth."""
        PersonalAccessToken.objects.create(user=self.user, token='pat_token')

        client = GitHubClient(self.user)
        token = client._get_token()

        self.assertEqual(token, 'pat_token')

    @patch('allauth.socialaccount.models.SocialToken')
    def test_get_token_falls_back_to_oauth(self, mock_social_token):
        """Verify OAuth used when no PAT."""
        mock_token = MagicMock()
        mock_token.token = 'oauth_token'
        mock_social_token.objects.filter.return_value.first.return_value = mock_token

        client = GitHubClient(self.user)
        token = client._get_token()

        self.assertEqual(token, 'oauth_token')


class GitHubClientErrorHandlingTests(TestCase):
    """Tests for GitHubClient error handling."""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.client = GitHubClient(self.user)

    def test_handle_api_error_rate_limit(self):
        """Verify rate limit handling."""
        from github import RateLimitExceededException

        error = RateLimitExceededException(403, {}, {})
        self.client._handle_api_error(error, 'owner', 'repo')

        self.assertIn('owner/repo', self.client._rate_limited_repos)

    def test_handle_api_error_404(self):
        """Verify not found handling."""
        error = MagicMock()
        error.status = 404
        self.client._handle_api_error(error, 'owner', 'repo')

        self.assertIn('Repository not found', self.client.errors[0])

    def test_grouped_errors_formatting(self):
        """Verify error grouping."""
        self.client._add_error('Access denied', 'org/repo1')
        self.client._add_error('Access denied', 'org/repo2')

        errors = self.client.errors
        self.assertEqual(len(errors), 1)
        self.assertIn('org/repo1', errors[0])
        self.assertIn('org/repo2', errors[0])

    def test_is_rate_limit_error_403(self):
        """Verify rate limit detection from 403 error."""
        error = MagicMock()
        error.status = 403
        error.data = {'message': 'API rate limit exceeded'}

        self.assertTrue(self.client._is_rate_limit_error(error))

    @patch('dashboard.github_client.time.sleep')
    @patch('dashboard.github_client.requests.post')
    def test_post_graphql_retries_truncated_response(self, mock_post, mock_sleep):
        response = MagicMock(status_code=200)
        response.json.return_value = {'data': {'viewer': {'login': 'testuser'}}}
        mock_post.side_effect = [
            requests.exceptions.ChunkedEncodingError('Response ended prematurely'),
            response,
        ]

        data = self.client._post_graphql('query { viewer { login } }', token='token')

        self.assertEqual(data, {'data': {'viewer': {'login': 'testuser'}}})
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once_with(0.5)


class GitHubClientCIStatusParsingTests(TestCase):
    """Additional tests for CI status parsing."""

    def _client(self):
        return GitHubClient(user=None)

    def _pr_data(self, rollup_state, contexts, check_suites=None):
        return {
            'number': 123,
            'commits': {
                'nodes': [
                    {
                        'commit': {
                            'statusCheckRollup': {
                                'state': rollup_state,
                                'contexts': contexts,
                            },
                            'checkSuites': check_suites or {'nodes': []},
                        }
                    }
                ]
            },
        }

    def test_parse_ci_status_pending(self):
        """Verify pending state detection."""
        contexts = {
            'totalCount': 2,
            'nodes': [
                {'conclusion': 'SUCCESS', 'status': 'COMPLETED'},
                {'conclusion': None, 'status': 'IN_PROGRESS'},
            ],
        }

        ci_status = self._client()._parse_ci_status_from_graphql(
            self._pr_data('PENDING', contexts)
        )

        self.assertEqual(ci_status.state, 'pending')

    def test_parse_ci_status_pending_approval(self):
        """Verify pending approval detection."""
        contexts = {
            'totalCount': 2,
            'nodes': [
                {'conclusion': 'SUCCESS', 'status': 'COMPLETED'},
                {'conclusion': 'SUCCESS', 'status': 'COMPLETED'},
            ],
        }
        check_suites = {
            'nodes': [
                {
                    'status': 'COMPLETED',
                    'conclusion': 'ACTION_REQUIRED',
                },
            ],
        }

        ci_status = self._client()._parse_ci_status_from_graphql(
            self._pr_data('SUCCESS', contexts, check_suites)
        )

        self.assertEqual(ci_status.state, 'success')
        self.assertTrue(ci_status.pending_approval)

    def test_parse_ci_status_all_skipped(self):
        """Verify all skipped = success."""
        contexts = {
            'totalCount': 3,
            'nodes': [
                {'conclusion': 'SKIPPED', 'status': 'COMPLETED'},
                {'conclusion': 'SKIPPED', 'status': 'COMPLETED'},
                {'conclusion': 'NEUTRAL', 'status': 'COMPLETED'},
            ],
        }

        ci_status = self._client()._parse_ci_status_from_graphql(
            self._pr_data('SUCCESS', contexts)
        )

        self.assertEqual(ci_status.state, 'success')

    def test_parse_ci_status_no_contexts(self):
        """Verify empty contexts handling."""
        ci_status = self._client()._parse_ci_status_from_graphql({
            'number': 123,
            'commits': {'nodes': []},
        })

        self.assertEqual(ci_status.state, 'unknown')


class GitHubClientReviewStatusParsingTests(TestCase):
    """Tests for review status parsing."""

    def _client(self):
        return GitHubClient(user=None)

    def test_parse_review_status_approved(self):
        """Verify approval detection."""
        pr_data = {
            'reviews': {
                'nodes': [
                    {'author': {'login': 'reviewer1'}, 'state': 'APPROVED', 'submittedAt': '2024-01-01T10:00:00Z'},
                ]
            },
            'comments': {'totalCount': 0},
            'reviewThreads': {'totalCount': 0},
        }

        status = self._client()._parse_review_status_from_graphql(pr_data)

        self.assertEqual(status.state, 'approved')
        self.assertEqual(status.approval_count, 1)

    def test_parse_review_status_changes_requested(self):
        """Verify changes_requested."""
        pr_data = {
            'reviews': {
                'nodes': [{
                    'author': {'login': 'reviewer1'},
                    'state': 'CHANGES_REQUESTED',
                    'submittedAt': '2024-01-01T10:00:00Z',
                }]
            },
            'comments': {'totalCount': 0},
            'reviewThreads': {'totalCount': 0},
        }

        status = self._client()._parse_review_status_from_graphql(pr_data)

        self.assertEqual(status.state, 'changes_requested')

    def test_parse_review_status_commented_doesnt_override(self):
        """Verify COMMENTED doesn't override APPROVED."""
        pr_data = {
            'reviews': {
                'nodes': [
                    {'author': {'login': 'reviewer1'}, 'state': 'APPROVED', 'submittedAt': '2024-01-01T10:00:00Z'},
                    {'author': {'login': 'reviewer1'}, 'state': 'COMMENTED', 'submittedAt': '2024-01-01T12:00:00Z'},
                ]
            },
            'comments': {'totalCount': 0},
            'reviewThreads': {'totalCount': 0},
        }

        status = self._client()._parse_review_status_from_graphql(pr_data)

        self.assertEqual(status.state, 'approved')
        self.assertEqual(status.approval_count, 1)

    def test_parse_review_latest_per_user(self):
        """Verify latest review per user."""
        pr_data = {
            'reviews': {
                'nodes': [
                    {
                        'author': {'login': 'reviewer1'},
                        'state': 'CHANGES_REQUESTED',
                        'submittedAt': '2024-01-01T10:00:00Z',
                    },
                    {
                        'author': {'login': 'reviewer1'},
                        'state': 'APPROVED',
                        'submittedAt': '2024-01-01T12:00:00Z',
                    },
                ]
            },
            'comments': {'totalCount': 0},
            'reviewThreads': {'totalCount': 0},
        }

        status = self._client()._parse_review_status_from_graphql(pr_data)

        self.assertEqual(status.state, 'approved')


class GitHubClientResponseSummarizationTests(TestCase):
    """Tests for response summarization."""

    def setUp(self):
        self.client = GitHubClient(user=None)

    def test_summarize_response_json_error(self):
        """Verify JSON error extraction."""
        response = MagicMock()
        response.json.return_value = {'message': 'Bad credentials'}

        summary = self.client._summarize_response(response)

        self.assertEqual(summary, 'Bad credentials')

    def test_summarize_response_html_page(self):
        """Verify HTML error page handling."""
        response = MagicMock()
        response.json.side_effect = ValueError('Not JSON')
        response.text = '<html><body>Error</body></html>'
        response.headers = {'Content-Type': 'text/html'}

        summary = self.client._summarize_response(response)

        self.assertEqual(summary, 'HTML error page omitted')

    def test_summarize_response_empty(self):
        """Verify empty response handling."""
        response = MagicMock()
        response.json.side_effect = ValueError('Not JSON')
        response.text = ''
        response.headers = {}

        summary = self.client._summarize_response(response)

        self.assertEqual(summary, 'empty response body')


class GitHubClientConsolidatedSearchTests(TestCase):
    """Verify multi-repo fetchers issue one search call per query, not one per repo."""

    def setUp(self):
        self.client = GitHubClient(user=None)
        self.repos = [('org', 'repo1'), ('org', 'repo2'), ('org', 'repo3')]
        self.client._get_token = MagicMock(return_value='token')
        self.client.get_username = MagicMock(return_value='testuser')

    @staticmethod
    def _empty_search_response():
        response = MagicMock(status_code=200)
        response.json.return_value = {'items': []}
        return response

    @patch('dashboard.github_client.requests.get')
    def test_get_all_merged_prs_issues_one_search_call(self, mock_get):
        mock_get.return_value = self._empty_search_response()

        result = self.client.get_all_merged_prs(self.repos, author='testuser')

        self.assertEqual(result, [])
        self.assertEqual(mock_get.call_count, 1)

    @patch('dashboard.github_client.requests.get')
    def test_get_all_assigned_prs_issues_one_search_call(self, mock_get):
        mock_get.return_value = self._empty_search_response()

        result = self.client.get_all_assigned_prs(self.repos)

        self.assertEqual(result, [])
        self.assertEqual(mock_get.call_count, 1)

    @patch('dashboard.github_client.requests.get')
    def test_get_all_review_requests_issues_one_search_call(self, mock_get):
        mock_get.return_value = self._empty_search_response()

        result = self.client.get_all_review_requests(self.repos)

        self.assertEqual(result, [])
        self.assertEqual(mock_get.call_count, 1)

    @patch('dashboard.github_client.requests.get')
    def test_get_all_review_requests_include_all_issues_two_search_calls(self, mock_get):
        mock_get.return_value = self._empty_search_response()

        result = self.client.get_all_review_requests(self.repos, include_all=True)

        self.assertEqual(result, [])
        self.assertEqual(mock_get.call_count, 2)

    @patch('dashboard.github_client.requests.get')
    def test_get_all_review_requests_include_all_unions_pending_and_reviewed(self, mock_get):
        def fake_get(url, params=None, headers=None, timeout=None):
            response = MagicMock(status_code=200)
            if 'review-requested' in params['q']:
                items = [{'number': 1, 'repository_url': 'https://api.github.com/repos/org/repo1'}]
            else:
                items = [{'number': 2, 'repository_url': 'https://api.github.com/repos/org/repo1'}]
            response.json.return_value = {'items': items}
            return response

        mock_get.side_effect = fake_get
        self.client._fetch_prs_multi_repo_graphql = MagicMock(return_value=[])

        self.client.get_all_review_requests(self.repos, include_all=True)

        self.client._fetch_prs_multi_repo_graphql.assert_called_once_with(
            {('org', 'repo1'): [1, 2]}
        )

    @patch('dashboard.github_client.requests.get')
    def test_search_prs_consolidated_paginates_past_first_page(self, mock_get):
        """A full first page must not silently truncate results at 100."""
        full_page = MagicMock(status_code=200)
        full_page.json.return_value = {
            'items': [
                {'number': i, 'repository_url': 'https://api.github.com/repos/org/repo1'}
                for i in range(100)
            ]
        }
        second_page = MagicMock(status_code=200)
        second_page.json.return_value = {
            'items': [{'number': 100, 'repository_url': 'https://api.github.com/repos/org/repo1'}]
        }
        mock_get.side_effect = [full_page, second_page]

        result = self.client._search_prs_consolidated('is:pr is:open author:testuser', self.repos)

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(result[('org', 'repo1')], list(range(101)))

    @patch('dashboard.github_client.requests.get')
    def test_search_prs_consolidated_non_rate_limit_403_reports_error(self, mock_get):
        response = MagicMock(status_code=403, text='SAML SSO enforcement required')
        response.json.side_effect = ValueError
        mock_get.return_value = response

        result = self.client._search_prs_consolidated('is:pr is:open author:testuser', self.repos)

        self.assertEqual(result, {})
        self.assertTrue(self.client.errors)


class GitHubClientBatchFetchConcurrencyTests(TestCase):
    """Verify PR-detail batches are fetched in parallel, not one after another."""

    SLEEP_SECONDS = 0.2

    def setUp(self):
        self.client = GitHubClient(user=None)

    def test_fetch_prs_multi_repo_graphql_runs_batches_concurrently(self):
        pr_data = {('org', 'repo1'): list(range(150))}  # 3 batches of 50
        sleep_seconds = self.SLEEP_SECONDS

        def slow_batch(prs):
            time.sleep(sleep_seconds)
            return []

        self.client._fetch_pr_batch_multi_repo = MagicMock(side_effect=slow_batch)

        start = time.monotonic()
        self.client._fetch_prs_multi_repo_graphql(pr_data)
        elapsed = time.monotonic() - start

        self.assertEqual(self.client._fetch_pr_batch_multi_repo.call_count, 3)
        self.assertLess(elapsed, sleep_seconds * 2)

    def test_fetch_prs_batch_graphql_runs_batches_concurrently(self):
        pr_numbers = list(range(GRAPHQL_PR_BATCH_SIZE * 3))  # 3 batches
        sleep_seconds = self.SLEEP_SECONDS

        original = GitHubClient._fetch_prs_batch_graphql

        def slow_fetch(client_self, owner, name, numbers):
            if len(numbers) > GRAPHQL_PR_BATCH_SIZE:
                return original(client_self, owner, name, numbers)
            time.sleep(sleep_seconds)
            return []

        with patch.object(GitHubClient, '_fetch_prs_batch_graphql', slow_fetch):
            start = time.monotonic()
            self.client._fetch_prs_batch_graphql('org', 'repo1', pr_numbers)
            elapsed = time.monotonic() - start

        self.assertLess(elapsed, sleep_seconds * 2)
