from django.test import TestCase

from .github_client import GitHubClient
from .views import _parse_repo_input


class RepoInputParserTests(TestCase):
    """Tests for repository input parsing."""

    def test_simple_owner_repo_format(self):
        """Test simple owner/repo format."""
        owner, name = _parse_repo_input('owner/repo')
        self.assertEqual(owner, 'owner')
        self.assertEqual(name, 'repo')

    def test_https_url(self):
        """Test HTTPS GitHub URL."""
        owner, name = _parse_repo_input('https://github.com/owner/repo')
        self.assertEqual(owner, 'owner')
        self.assertEqual(name, 'repo')

    def test_https_url_with_git_suffix(self):
        """Test HTTPS GitHub URL with .git suffix."""
        owner, name = _parse_repo_input('https://github.com/owner/repo.git')
        self.assertEqual(owner, 'owner')
        self.assertEqual(name, 'repo')

    def test_ssh_url(self):
        """Test SSH GitHub URL."""
        owner, name = _parse_repo_input('git@github.com:owner/repo.git')
        self.assertEqual(owner, 'owner')
        self.assertEqual(name, 'repo')

    def test_url_with_trailing_slash(self):
        """Test URL with trailing slash."""
        owner, name = _parse_repo_input('https://github.com/owner/repo/')
        self.assertEqual(owner, 'owner')
        self.assertEqual(name, 'repo')

    def test_owner_repo_with_git_suffix(self):
        """Test owner/repo with .git suffix."""
        owner, name = _parse_repo_input('owner/repo.git')
        self.assertEqual(owner, 'owner')
        self.assertEqual(name, 'repo')

    def test_whitespace_trimming(self):
        """Test that whitespace is properly trimmed."""
        owner, name = _parse_repo_input('  owner/repo  ')
        self.assertEqual(owner, 'owner')
        self.assertEqual(name, 'repo')

    def test_invalid_no_slash(self):
        """Test invalid input without slash."""
        owner, name = _parse_repo_input('invalid')
        self.assertIsNone(owner)
        self.assertIsNone(name)

    def test_invalid_empty_owner(self):
        """Test invalid input with empty owner."""
        owner, name = _parse_repo_input('/repo')
        self.assertEqual(owner, '')
        self.assertEqual(name, 'repo')

    def test_invalid_empty_repo(self):
        """Test invalid input with empty repo."""
        owner, name = _parse_repo_input('owner/')
        self.assertEqual(owner, 'owner')
        self.assertEqual(name, '')


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
