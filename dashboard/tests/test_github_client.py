from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from dashboard.github_client import GitHubClient
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


class GitHubClientCIStatusParsingTests(TestCase):
    """Additional tests for CI status parsing."""

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
