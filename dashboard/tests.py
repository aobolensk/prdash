from django.test import TestCase
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
