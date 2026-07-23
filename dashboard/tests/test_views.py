import json
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from dashboard.models import PersonalAccessToken, TrackedRepository
from dashboard.views import _parse_days_param, _parse_repo_input


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

    def test_repo_name_ending_in_git_chars(self):
        """Test repo names ending in chars from '.git' set aren't truncated."""
        owner, name = _parse_repo_input('owner/my-project')
        self.assertEqual(owner, 'owner')
        self.assertEqual(name, 'my-project')

    def test_repo_name_ending_in_git(self):
        """Test repo name ending in 'git' (but not '.git') is preserved."""
        owner, name = _parse_repo_input('owner/test-git')
        self.assertEqual(owner, 'owner')
        self.assertEqual(name, 'test-git')

    def test_repo_name_dotgit(self):
        """Test repo name 'dotgit' is preserved."""
        owner, name = _parse_repo_input('owner/dotgit')
        self.assertEqual(owner, 'owner')
        self.assertEqual(name, 'dotgit')


class HomeViewTests(TestCase):
    """Tests for home view."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass')

    def test_home_unauthenticated(self):
        """Verify home page renders for anonymous users."""
        response = self.client.get(reverse('dashboard:home'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dashboard/home.html')

    def test_home_authenticated_redirects(self):
        """Verify redirect to pr_list when logged in."""
        self.client.login(username='testuser', password='testpass')
        response = self.client.get(reverse('dashboard:home'))
        self.assertRedirects(response, reverse('dashboard:pr_list'))


class AuthenticationRequiredTests(TestCase):
    """Tests for login-required views."""

    def setUp(self):
        self.client = Client()

    def test_pr_list_requires_login(self):
        """Verify login required for PR list."""
        response = self.client.get(reverse('dashboard:pr_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_settings_requires_login(self):
        """Verify login required for settings."""
        response = self.client.get(reverse('dashboard:settings'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_stats_requires_login(self):
        """Verify login required for stats."""
        response = self.client.get(reverse('dashboard:stats'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)


class RepositoryManagementTests(TestCase):
    """Tests for repository management views."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.client.login(username='testuser', password='testpass')

    @patch('dashboard.views.GitHubClient')
    def test_add_repo_valid_format(self, mock_github_client):
        """Verify adding repo with owner/repo format."""
        mock_client = MagicMock()
        mock_client.validate_repo.return_value = (True, 'Found: owner/repo')
        mock_github_client.return_value = mock_client

        response = self.client.post(
            reverse('dashboard:add_repo'),
            {'repo': 'owner/repo'},
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(TrackedRepository.objects.filter(
            user=self.user, owner='owner', name='repo'
        ).exists())

    @patch('dashboard.views.GitHubClient')
    def test_add_repo_https_url(self, mock_github_client):
        """Verify adding repo via HTTPS URL."""
        mock_client = MagicMock()
        mock_client.validate_repo.return_value = (True, 'Found')
        mock_github_client.return_value = mock_client

        response = self.client.post(
            reverse('dashboard:add_repo'),
            {'repo': 'https://github.com/myowner/myrepo'},
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(TrackedRepository.objects.filter(
            user=self.user, owner='myowner', name='myrepo'
        ).exists())

    def test_add_repo_invalid_format(self):
        """Verify error on invalid input."""
        response = self.client.post(
            reverse('dashboard:add_repo'),
            {'repo': 'invalid'},
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('showErrors', response['HX-Trigger'])
        self.assertFalse(TrackedRepository.objects.filter(user=self.user).exists())

    @patch('dashboard.views.GitHubClient')
    def test_add_repo_duplicate(self, mock_github_client):
        """Verify error on duplicate repo."""
        mock_client = MagicMock()
        mock_client.validate_repo.return_value = (True, 'Found')
        mock_github_client.return_value = mock_client

        TrackedRepository.objects.create(user=self.user, owner='owner', name='repo')

        response = self.client.post(
            reverse('dashboard:add_repo'),
            {'repo': 'owner/repo'},
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('showErrors', response['HX-Trigger'])
        self.assertEqual(TrackedRepository.objects.filter(user=self.user).count(), 1)

    def test_remove_repo_success(self):
        """Verify repo deletion."""
        repo = TrackedRepository.objects.create(user=self.user, owner='owner', name='repo')

        response = self.client.post(
            reverse('dashboard:remove_repo', args=[repo.id]),
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TrackedRepository.objects.filter(id=repo.id).exists())

    def test_remove_repo_not_owned(self):
        """Verify can't delete other user's repo."""
        other_user = User.objects.create_user(username='other', password='pass')
        repo = TrackedRepository.objects.create(user=other_user, owner='owner', name='repo')

        response = self.client.post(
            reverse('dashboard:remove_repo', args=[repo.id]),
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(TrackedRepository.objects.filter(id=repo.id).exists())


class HTMXResponseTests(TestCase):
    """Tests for HTMX response handling."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.client.login(username='testuser', password='testpass')
        cache.clear()

    @patch('dashboard.views.GitHubClient')
    def test_pr_list_htmx_returns_partial(self, mock_github_client):
        """Verify partial template for HX-Request."""
        mock_client = MagicMock()
        mock_client.get_all_user_prs.return_value = []
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_client.get_notification_triggers.return_value = {}
        mock_github_client.return_value = mock_client

        response = self.client.get(
            reverse('dashboard:pr_list'),
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dashboard/partials/_pr_content.html')

    @patch('dashboard.views.GitHubClient')
    def test_pr_list_full_page(self, mock_github_client):
        """Verify full template without HX-Request."""
        mock_client = MagicMock()
        mock_client.get_all_user_prs.return_value = []
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_github_client.return_value = mock_client

        response = self.client.get(reverse('dashboard:pr_list'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dashboard/pr_list.html')

    @patch('dashboard.views.GitHubClient')
    def test_htmx_trigger_headers(self, mock_github_client):
        """Verify HX-Trigger headers set correctly."""
        mock_client = MagicMock()
        mock_client.get_all_user_prs.return_value = []
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_client.get_notification_triggers.return_value = {}
        mock_github_client.return_value = mock_client

        response = self.client.get(
            reverse('dashboard:pr_list'),
            HTTP_HX_REQUEST='true'
        )

        self.assertIn('HX-Trigger', response.headers)
        triggers = json.loads(response['HX-Trigger'])
        self.assertIn('tabChanged', triggers)

    @patch('dashboard.views.GitHubClient')
    def test_pr_list_htmx_repeat_poll_with_no_changes_skips_render(self, mock_github_client):
        """A second poll with unchanged PRs should return 204 instead of re-rendering."""
        mock_client = MagicMock()
        mock_client.get_all_user_prs.return_value = []
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_client.get_notification_triggers.return_value = {}
        mock_github_client.return_value = mock_client

        first = self.client.get(
            reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true', HTTP_HX_TRIGGER='auto-refresh-container'
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.get(
            reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true', HTTP_HX_TRIGGER='auto-refresh-container'
        )
        self.assertEqual(second.status_code, 204)
        self.assertEqual(second['HX-Reswap'], 'none')

    @patch('dashboard.views.GitHubClient')
    def test_pr_list_htmx_repeat_navigation_with_no_changes_still_renders(self, mock_github_client):
        """Tab/filter navigation (not an auto-refresh poll) must never be skipped, even
        if it happens to revisit a URL an auto-refresh poll already rendered."""
        mock_client = MagicMock()
        mock_client.get_all_user_prs.return_value = []
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_client.get_notification_triggers.return_value = {}
        mock_github_client.return_value = mock_client

        first = self.client.get(
            reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true', HTTP_HX_TRIGGER='auto-refresh-container'
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.get(reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true')
        self.assertEqual(second.status_code, 200)
        self.assertTemplateUsed(second, 'dashboard/partials/_pr_content.html')

    @patch('dashboard.views.GitHubClient')
    def test_pr_list_htmx_repeat_poll_with_changes_rerenders(self, mock_github_client):
        """A second poll with changed PR data should still return the full partial."""
        mock_client = MagicMock()
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_client.get_notification_triggers.return_value = {}
        mock_github_client.return_value = mock_client

        mock_client.get_all_user_prs.return_value = []
        first = self.client.get(
            reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true', HTTP_HX_TRIGGER='auto-refresh-container'
        )
        self.assertEqual(first.status_code, 200)

        from dashboard.github_client import PullRequestInfo, CIStatus, ReviewStatus
        from datetime import datetime, timezone

        mock_client.get_all_user_prs.return_value = [
            PullRequestInfo(
                number=1,
                title='Test PR',
                url='https://github.com/o/r/pull/1',
                repo_owner='o',
                repo_name='r',
                author='someone',
                author_avatar='',
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                labels=[],
                ci_status=CIStatus(state='success'),
                review_status=ReviewStatus(state='not_reviewed'),
                draft=False,
                additions=1,
                deletions=1,
            )
        ]
        second = self.client.get(
            reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true', HTTP_HX_TRIGGER='auto-refresh-container'
        )
        self.assertEqual(second.status_code, 200)
        self.assertTemplateUsed(second, 'dashboard/partials/_pr_content.html')


class PATManagementTests(TestCase):
    """Tests for Personal Access Token management."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.client.login(username='testuser', password='testpass')

    @patch('dashboard.views.requests.get')
    def test_save_pat_valid_token(self, mock_get):
        """Verify saving valid token."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        response = self.client.post(
            reverse('dashboard:save_pat'),
            {'token': 'ghp_validtoken123456'},
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(PersonalAccessToken.objects.filter(user=self.user).exists())

    @patch('dashboard.views.requests.get')
    def test_save_pat_invalid_token(self, mock_get):
        """Verify error on invalid token."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        response = self.client.post(
            reverse('dashboard:save_pat'),
            {'token': 'invalid_token'},
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PersonalAccessToken.objects.filter(user=self.user).exists())

    @patch('dashboard.views.requests.get')
    def test_save_pat_empty_deletes(self, mock_get):
        """Verify empty token deletes existing."""
        PersonalAccessToken.objects.create(user=self.user, token='existing_token')

        response = self.client.post(
            reverse('dashboard:save_pat'),
            {'token': ''},
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PersonalAccessToken.objects.filter(user=self.user).exists())

    def test_delete_pat_success(self):
        """Verify PAT deletion."""
        PersonalAccessToken.objects.create(user=self.user, token='some_token')

        response = self.client.post(
            reverse('dashboard:delete_pat'),
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PersonalAccessToken.objects.filter(user=self.user).exists())


class DaysParamParsingTests(TestCase):
    """Tests for _parse_days_param function."""

    def test_parse_days_param_valid_values(self):
        """Test valid day values."""
        self.assertEqual(_parse_days_param('7'), 7)
        self.assertEqual(_parse_days_param('14'), 14)
        self.assertEqual(_parse_days_param('30'), 30)
        self.assertEqual(_parse_days_param('90'), 90)
        self.assertEqual(_parse_days_param('180'), 180)
        self.assertEqual(_parse_days_param('365'), 365)

    def test_parse_days_param_all(self):
        """Test 'all' returns -1."""
        self.assertEqual(_parse_days_param('all'), -1)

    def test_parse_days_param_invalid(self):
        """Test invalid defaults to 30."""
        self.assertEqual(_parse_days_param('invalid'), 30)
        self.assertEqual(_parse_days_param('15'), 30)  # Not in allowed list
        self.assertEqual(_parse_days_param(''), 30)
        self.assertEqual(_parse_days_param(None), 30)
