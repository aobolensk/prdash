import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from dashboard.github_client import CIStatus, PullRequestInfo, ReviewStatus
from dashboard.models import PersonalAccessToken, TrackedRepository, UserPreferences
from dashboard import views
from dashboard.views import _parse_repo_input


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

    def test_https_url_with_dot_in_repo_name(self):
        """Test HTTPS URL where the repo name itself contains a dot."""
        owner, name = _parse_repo_input('https://github.com/foo/bar.baz')
        self.assertEqual(owner, 'foo')
        self.assertEqual(name, 'bar.baz')

    def test_https_url_with_leading_dot_repo_name(self):
        """Test HTTPS URL for a repo name starting with a dot."""
        owner, name = _parse_repo_input('https://github.com/foo/.github')
        self.assertEqual(owner, 'foo')
        self.assertEqual(name, '.github')


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


class RepositoryManagementTests(TestCase):
    """Tests for repository management views."""

    count_tabs = ('my_prs', 'review_requests', 'assigned')

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.client.login(username='testuser', password='testpass')
        cache.clear()

    def _prime_pr_caches(self):
        cache.set(f'pr_results_gen:{self.user.id}', 7)
        cache.set(f'pr_results:{self.user.id}:7:/prs/:test', ['stale PR snapshot'])
        for tab in self.count_tabs:
            cache.set(f'pr_count:{self.user.id}:{tab}', 10)

    def _assert_pr_caches_invalidated(self):
        self.assertEqual(cache.get(f'pr_results_gen:{self.user.id}'), 8)
        self.assertIsNone(cache.get(f'pr_results:{self.user.id}:8:/prs/:test'))
        for tab in self.count_tabs:
            self.assertIsNone(cache.get(f'pr_count:{self.user.id}:{tab}'))

    @patch('dashboard.views.GitHubClient')
    def test_add_repo_valid_format(self, mock_github_client):
        """Verify adding repo with owner/repo format."""
        self._prime_pr_caches()
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
        self._assert_pr_caches_invalidated()

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
        self._prime_pr_caches()
        repo = TrackedRepository.objects.create(user=self.user, owner='owner', name='repo')

        response = self.client.post(
            reverse('dashboard:remove_repo', args=[repo.id]),
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TrackedRepository.objects.filter(id=repo.id).exists())
        self._assert_pr_caches_invalidated()

    def test_toggle_repo_success(self):
        """Verify toggling a repo invalidates PR caches."""
        self._prime_pr_caches()
        repo = TrackedRepository.objects.create(user=self.user, owner='owner', name='repo')

        response = self.client.post(
            reverse('dashboard:toggle_repo', args=[repo.id]),
            HTTP_HX_REQUEST='true'
        )

        self.assertEqual(response.status_code, 200)
        repo.refresh_from_db()
        self.assertFalse(repo.enabled)
        self._assert_pr_caches_invalidated()

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
        self.assertIn('no-store', response['Cache-Control'])

    @patch('dashboard.views.GitHubClient')
    def test_tracked_repo_link_preserves_tab_urls_and_repo_scope(self, mock_github_client):
        """Render repository navigation and fetch only the selected repository."""
        TrackedRepository.objects.create(user=self.user, owner='owner', name='repo')
        mock_client = MagicMock()
        mock_client.get_all_user_prs.return_value = []
        mock_client.get_user_prs_for_repo.return_value = []
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_client.get_notification_triggers.return_value = {}
        mock_github_client.return_value = mock_client

        response = self.client.get(reverse('dashboard:pr_list'))
        content = response.content.decode()
        repo_urls = {
            'my_prs': reverse('dashboard:repo_pr_list', args=['owner', 'repo']),
            'merged': reverse('dashboard:repo_merged_pr_list', args=['owner', 'repo']),
            'review_requests': reverse('dashboard:repo_review_requests_list', args=['owner', 'repo']),
            'assigned': reverse('dashboard:repo_assigned_list', args=['owner', 'repo']),
        }

        self.assertContains(response, 'class="repo-link')
        self.assertContains(response, f'href="{repo_urls["my_prs"]}"')
        self.assertContains(response, 'data-repo="owner/repo"')
        self.assertContains(response, f'hx-get="{repo_urls["my_prs"]}"')
        self.assertContains(response, 'hx-target="#pr-content"')
        self.assertContains(response, 'hx-swap="innerHTML"')
        self.assertContains(response, 'hx-push-url="true"')
        for tab, url in repo_urls.items():
            self.assertIn(f'data-url-{tab}="{url}"', content)

        response = self.client.get(repo_urls['my_prs'], HTTP_HX_REQUEST='true')

        self.assertEqual(response.status_code, 200)
        mock_client.get_user_prs_for_repo.assert_called_once_with('owner', 'repo')

    @patch('dashboard.views.GitHubClient')
    def test_author_pr_list_fetches_selected_author(self, mock_github_client):
        """Verify the author tab fetches open PRs for the selected author."""
        mock_client = MagicMock()
        mock_client.get_all_user_prs.return_value = []
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_github_client.return_value = mock_client

        response = self.client.get(
            reverse('dashboard:author_pr_list'),
            {'author': 'octocat'},
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dashboard/partials/_pr_content.html')
        mock_client.get_all_user_prs.assert_called_once_with([], author='octocat')
        self.assertContains(response, 'Pull Requests by octocat')

        page = self.client.get(reverse('dashboard:author_pr_list'), {'author': 'octocat'})

        self.assertTemplateUsed(page, 'dashboard/pr_list.html')
        self.assertContains(page, 'PRs by Author')
        self.assertContains(page, '<title>PRs by Author - PR Dashboard</title>', html=True)
        self.assertContains(page, 'hx-get="/prs/by-author/?author=octocat" hx-trigger="load"')
        self.assertContains(page, 'class="filter-toolbar"')
        self.assertContains(page, 'Pull Requests by octocat')
        self.assertContains(page, 'Loading pull requests')

    @patch('dashboard.views.GitHubClient')
    def test_author_pr_list_without_author_does_not_fetch(self, mock_github_client):
        """Verify the author tab prompts for an author before querying GitHub."""
        mock_client = MagicMock()
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_github_client.return_value = mock_client

        response = self.client.get(reverse('dashboard:author_pr_list'), HTTP_HX_REQUEST='true')

        self.assertEqual(response.status_code, 200)
        mock_client.get_all_user_prs.assert_not_called()
        self.assertContains(response, 'Enter a GitHub username to view their pull requests.')

    @patch('dashboard.views.GitHubClient')
    def test_author_merged_pr_list_fetches_selected_author(self, mock_github_client):
        """Verify the author tab can show merged PRs for the selected author."""
        mock_client = MagicMock()
        mock_client.get_all_merged_prs.return_value = []
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_github_client.return_value = mock_client

        response = self.client.get(
            reverse('dashboard:author_merged_pr_list'),
            {'author': 'octocat'},
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        mock_client.get_all_merged_prs.assert_called_once_with([], author='octocat')
        self.assertContains(response, 'class="tab active"')

    @patch('dashboard.views.GitHubClient')
    def test_pr_list_pagination_has_single_direct_page_input(self, mock_github_client):
        """Verify the current page is an editable page-number input."""
        UserPreferences.objects.create(user=self.user, pr_list_page_size=25)
        mock_client = MagicMock()
        now = timezone.now()
        mock_client.get_all_user_prs.return_value = [
            PullRequestInfo(
                number=number,
                title=f'PR {number}',
                url=f'https://github.com/owner/repo/pull/{number}',
                repo_owner='owner',
                repo_name='repo',
                author='testuser',
                author_avatar='',
                created_at=now,
                updated_at=now,
                labels=[],
                ci_status=CIStatus(state='unknown'),
                review_status=ReviewStatus(state='not_reviewed'),
                draft=False,
                additions=0,
                deletions=0,
            )
            for number in range(1, 27)
        ]
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_github_client.return_value = mock_client

        response = self.client.get(reverse('dashboard:pr_list'), {'page': 2}, HTTP_HX_REQUEST='true')
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertIn(f'<form action="{reverse("dashboard:pr_list")}"', content)
        self.assertEqual(content.count('id="page-input"'), 1)
        self.assertIn('type="number"', content)
        self.assertIn('value="2"', content)
        self.assertIn('max="2"', content)
        self.assertIn('aria-label="Page number"', content)

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
        self.assertEqual(triggers['pageTitle'], 'My PRs - PR Dashboard')

    @patch('dashboard.views.GitHubClient')
    def test_author_pr_list_htmx_sets_browser_title(self, mock_github_client):
        """Verify the author tab updates the browser title after an HTMX swap."""
        mock_client = MagicMock()
        mock_client.get_all_user_prs.return_value = []
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_client.get_notification_triggers.return_value = {}
        mock_github_client.return_value = mock_client

        response = self.client.get(
            reverse('dashboard:author_pr_list'),
            HTTP_HX_REQUEST='true',
        )

        triggers = json.loads(response['HX-Trigger'])
        self.assertEqual(triggers['pageTitle'], 'PRs by Author - PR Dashboard')
        self.assertContains(response, 'autocomplete="off"')

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
        self.assertContains(first, f'data-render-hash="{first.context["pr_render_hash"]}"')

        second = self.client.get(
            reverse('dashboard:pr_list'),
            HTTP_HX_REQUEST='true',
            HTTP_HX_TRIGGER='auto-refresh-container',
            HTTP_X_PR_RENDER_HASH=first.context['pr_render_hash'],
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
        older_browser = Client()
        older_browser.login(username='testuser', password='testpass')
        old_content = older_browser.get(reverse('dashboard:pr_list'), HTTP_HX_REQUEST='true')

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
            reverse('dashboard:pr_list'),
            HTTP_HX_REQUEST='true',
            HTTP_HX_TRIGGER='auto-refresh-container',
            HTTP_X_PR_RENDER_HASH=first.context['pr_render_hash'],
        )
        self.assertEqual(second.status_code, 200)
        self.assertTemplateUsed(second, 'dashboard/partials/_pr_content.html')

        older_poll = older_browser.get(
            reverse('dashboard:pr_list'),
            HTTP_HX_REQUEST='true',
            HTTP_HX_TRIGGER='auto-refresh-container',
            HTTP_X_PR_RENDER_HASH=old_content.context['pr_render_hash'],
        )
        self.assertContains(older_poll, 'Test PR')


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


class PluginPrListPageViewTests(TestCase):

    PAGE = {
        'plugin_id': 'page-plugin', 'name': 'triage', 'title': 'Triage', 'source': 'open_all',
        'fetch_on_demand': False, 'tab': 'plugin:page-plugin:triage',
    }

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.client.login(username='testuser', password='testpass')
        cache.clear()
        for name, value in (('pr_list_pages', [self.PAGE]), ('pr_list_page', self.PAGE)):
            patcher = patch(f'dashboard.views.plugin_manager.{name}', return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.url = reverse('dashboard:plugin_pr_list', args=['page-plugin', 'triage'])

    def mock_client(self, mock_github_client, prs):
        mock_client = MagicMock()
        mock_client.get_all_open_prs.return_value = prs
        mock_client.get_username.return_value = 'testuser'
        mock_client.errors = []
        mock_client.warnings = []
        mock_client.get_notification_triggers.return_value = {}
        mock_github_client.return_value = mock_client
        return mock_client

    @staticmethod
    def make_pr(number, author='someone', annotations=None):
        from datetime import datetime, timezone
        return PullRequestInfo(
            number=number, title=f'PR {number}', url=f'https://github.com/o/r/pull/{number}',
            repo_owner='o', repo_name='r', author=author, author_avatar='',
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            labels=[], ci_status=CIStatus(state='success'), review_status=ReviewStatus(state='not_reviewed'),
            draft=False, additions=1, deletions=1, plugin_annotations=annotations or {},
        )

    @patch('dashboard.views.GitHubClient')
    def test_full_page_lists_open_prs_of_others_and_links_the_page(self, mock_github_client):
        TrackedRepository.objects.create(user=self.user, owner='o', name='r')
        mock_client = self.mock_client(mock_github_client, [self.make_pr(1), self.make_pr(2, author='testuser')])

        content = self.client.get(self.url, HTTP_HX_REQUEST='true')
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        mock_client.get_all_open_prs.assert_called_once_with([('o', 'r')], exclude_author='testuser')
        self.assertContains(content, 'PR 1')
        self.assertNotContains(content, 'PR 2')
        self.assertContains(response, 'data-current-tab="plugin:page-plugin:triage"')
        self.assertContains(response, 'data-tab="plugin:page-plugin:triage"')
        self.assertContains(response, f'hx-get="{self.url}"')
        self.assertContains(response, '<title>Triage')

    @patch('dashboard.views.GitHubClient')
    def test_unknown_page_returns_404(self, mock_github_client):
        self.mock_client(mock_github_client, [])
        with patch('dashboard.views.plugin_manager.pr_list_page', return_value=None):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 404)

    @patch('dashboard.views.GitHubClient')
    def test_poll_rerenders_when_only_an_annotation_changes(self, mock_github_client):
        TrackedRepository.objects.create(user=self.user, owner='o', name='r')
        mock_client = self.mock_client(mock_github_client, [self.make_pr(1, annotations={'page-plugin': 1})])
        first = self.client.get(self.url, HTTP_HX_REQUEST='true', HTTP_HX_TRIGGER='auto-refresh-container')
        poll_headers = {
            'HTTP_HX_REQUEST': 'true',
            'HTTP_HX_TRIGGER': 'auto-refresh-container',
            'HTTP_X_PR_RENDER_HASH': first.context['pr_render_hash'],
        }

        unchanged = self.client.get(self.url, **poll_headers)
        cache.clear()
        mock_client.get_all_open_prs.return_value = [self.make_pr(1, annotations={'page-plugin': 2})]
        changed = self.client.get(self.url, **poll_headers)

        self.assertEqual(unchanged.status_code, 204)
        self.assertEqual(changed.status_code, 200)

    def test_plugin_page_auto_refresh_is_saved_and_used(self):
        UserPreferences.objects.create(user=self.user, plugin_auto_refresh={'plugin:gone:page': {'enabled': True}})

        response = self.client.post(reverse('dashboard:save_preferences'), {
            'auto_refresh_plugin:page-plugin:triage': 'on',
            'auto_refresh_interval_plugin:page-plugin:triage': '10',
        })

        prefs = UserPreferences.objects.get(user=self.user)
        self.assertEqual(prefs.plugin_auto_refresh, {
            'plugin:gone:page': {'enabled': True},
            'plugin:page-plugin:triage': {'enabled': True, 'interval': 10},
        })
        self.assertTrue(prefs.is_auto_refresh_enabled_for_tab('plugin:page-plugin:triage'))
        self.assertEqual(prefs.get_auto_refresh_interval_for_tab('plugin:page-plugin:triage'), 10)
        self.assertEqual(prefs.get_auto_refresh_interval_for_tab('plugin:gone:page'), 5)
        self.assertFalse(prefs.is_auto_refresh_enabled_for_tab('plugin:other:page'))
        self.assertContains(response, 'name="auto_refresh_plugin:page-plugin:triage"')
        self.assertContains(response, 'Triage')


class FetchOnDemandPageTests(PluginPrListPageViewTests):
    PAGE = {**PluginPrListPageViewTests.PAGE, 'fetch_on_demand': True}

    def setUp(self):
        super().setUp()
        views._pr_fetch_jobs.clear()
        self.addCleanup(views._pr_fetch_jobs.clear)
        self.fetch_url = reverse('dashboard:plugin_pr_list_fetch', args=['page-plugin', 'triage'])
        TrackedRepository.objects.create(user=self.user, owner='o', name='r')

    def fetch(self):
        response = self.client.post(self.fetch_url)
        for job in list(views._pr_fetch_jobs.values()):
            job['thread'].join(5)
        return response

    @patch('dashboard.views.GitHubClient')
    def test_page_does_not_fetch_until_asked(self, mock_github_client):
        mock_client = self.mock_client(mock_github_client, [self.make_pr(1)])

        response = self.client.get(self.url)
        status = self.client.get(self.fetch_url).json()

        mock_client.get_all_open_prs.assert_not_called()
        self.assertContains(response, 'id="pr-fetch-bar"')
        self.assertContains(response, 'Not fetched yet')
        self.assertNotContains(response, 'PR 1')
        self.assertEqual(status, {'state': 'idle'})

    @patch('dashboard.views.GitHubClient')
    def test_fetch_reports_progress_and_the_page_renders_its_result(self, mock_github_client):
        mock_client = self.mock_client(mock_github_client, [self.make_pr(1), self.make_pr(2, author='testuser')])

        def get_all_open_prs(repos, exclude_author=None, progress=None):
            progress('Searching repositories', 1, 1)
            return [self.make_pr(1), self.make_pr(2, author='testuser')]

        mock_client.get_all_open_prs.side_effect = get_all_open_prs
        started = self.fetch()
        status = self.client.get(self.fetch_url).json()
        page = self.client.get(self.url)

        self.assertEqual(started.status_code, 200)
        self.assertEqual(mock_client.get_all_open_prs.call_count, 1)
        self.assertEqual(status['state'], 'done')
        self.assertEqual((status['phase'], status['done'], status['total'], status['count']),
                         ('Searching repositories', 1, 1, 2))
        self.assertContains(page, 'PR 1')
        self.assertNotContains(page, 'PR 2')
        self.assertContains(page, 'fetched just now')
        self.assertEqual(mock_client.get_all_open_prs.call_count, 1)

    @patch('dashboard.views.GitHubClient')
    def test_refresh_after_a_fetch_rerenders_even_without_new_prs(self, mock_github_client):
        self.mock_client(mock_github_client, [])
        poll = {'HTTP_HX_REQUEST': 'true', 'HTTP_HX_TRIGGER': 'auto-refresh-container'}
        before = self.client.get(self.url, **poll)

        self.fetch()
        after = self.client.get(self.url, HTTP_X_PR_RENDER_HASH=before.context['pr_render_hash'], **poll)

        self.assertEqual(after.status_code, 200)
        self.assertContains(after, '0 PRs')

    @patch('dashboard.views.GitHubClient')
    def test_partial_fetch_keeps_the_previous_complete_list(self, mock_github_client):
        mock_client = self.mock_client(mock_github_client, [self.make_pr(1)])
        self.fetch()
        mock_client.get_all_open_prs.return_value = []
        mock_client.warnings = ['Rate limit hit. Not loaded: o/r']

        self.fetch()
        status = self.client.get(self.fetch_url).json()
        page = self.client.get(self.url)

        self.assertEqual(status['warnings'], ['Rate limit hit. Not loaded: o/r'])
        self.assertContains(page, 'PR 1')

    @patch('dashboard.views.GitHubClient')
    def test_failed_fetch_is_reported(self, mock_github_client):
        mock_client = self.mock_client(mock_github_client, [])
        mock_client.get_all_open_prs.side_effect = RuntimeError('boom')

        with self.assertLogs('dashboard.views', level='ERROR'):
            self.fetch()
        status = self.client.get(self.fetch_url).json()

        self.assertEqual((status['state'], status['errors']), ('failed', ['Fetching pull requests failed']))

    @patch('dashboard.views.GitHubClient')
    def test_fetch_endpoint_is_only_for_fetch_on_demand_pages(self, mock_github_client):
        self.mock_client(mock_github_client, [])
        with patch('dashboard.views.plugin_manager.pr_list_page', return_value=PluginPrListPageViewTests.PAGE):
            response = self.client.post(self.fetch_url)

        self.assertEqual(response.status_code, 404)

    # These inherited tests assume a fetch on every load.
    test_full_page_lists_open_prs_of_others_and_links_the_page = None
    test_poll_rerenders_when_only_an_annotation_changes = None
    test_plugin_page_auto_refresh_is_saved_and_used = None
    test_unknown_page_returns_404 = None


class PluginAssetCachingTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.asset = Path(directory.name) / 'app.js'
        self.asset.write_text('window.x = 1;', encoding='utf-8')
        patcher = patch('dashboard.views.plugin_manager.asset_path', return_value=self.asset)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = reverse('dashboard:plugin_asset', args=['p', 'app.js'])

    def test_serves_last_modified_and_honours_if_modified_since(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        last_modified = response['Last-Modified']

        cached = self.client.get(self.url, HTTP_IF_MODIFIED_SINCE=last_modified)
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached['Last-Modified'], last_modified)

        stale = self.client.get(self.url, HTTP_IF_MODIFIED_SINCE='Mon, 01 Jan 2001 00:00:00 GMT')
        self.assertEqual(stale.status_code, 200)
