from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase

from dashboard.models import PersonalAccessToken, TrackedRepository


class PersonalAccessTokenModelTests(TestCase):
    """Tests for PersonalAccessToken model."""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')

    def test_pat_str_representation(self):
        """Verify __str__ returns expected format."""
        pat = PersonalAccessToken.objects.create(user=self.user, token='ghp_testtoken123456')
        self.assertEqual(str(pat), 'PAT for testuser')

    def test_pat_get_masked_token_long(self):
        """Verify masking for tokens >8 chars."""
        pat = PersonalAccessToken.objects.create(user=self.user, token='ghp_testtoken123456')
        self.assertEqual(pat.get_masked_token(), 'ghp_...3456')

    def test_pat_get_masked_token_short(self):
        """Verify masking for tokens <=8 chars."""
        pat = PersonalAccessToken.objects.create(user=self.user, token='short')
        self.assertEqual(pat.get_masked_token(), '****')

    def test_pat_one_to_one_relationship(self):
        """Verify only one PAT per user."""
        PersonalAccessToken.objects.create(user=self.user, token='token1')
        with self.assertRaises(IntegrityError):
            PersonalAccessToken.objects.create(user=self.user, token='token2')


class TrackedRepositoryModelTests(TestCase):
    """Tests for TrackedRepository model."""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')

    def test_tracked_repo_str_representation(self):
        """Verify __str__ format."""
        repo = TrackedRepository.objects.create(user=self.user, owner='owner', name='repo')
        self.assertEqual(str(repo), 'owner/repo')

    def test_tracked_repo_full_name_property(self):
        """Verify full_name property."""
        repo = TrackedRepository.objects.create(user=self.user, owner='myorg', name='myrepo')
        self.assertEqual(repo.full_name, 'myorg/myrepo')

    def test_tracked_repo_unique_together(self):
        """Verify user+owner+name uniqueness constraint."""
        TrackedRepository.objects.create(user=self.user, owner='owner', name='repo')
        with self.assertRaises(IntegrityError):
            TrackedRepository.objects.create(user=self.user, owner='owner', name='repo')

    def test_tracked_repo_ordering(self):
        """Verify default ordering by owner, name."""
        TrackedRepository.objects.create(user=self.user, owner='zorg', name='alpha')
        TrackedRepository.objects.create(user=self.user, owner='aorg', name='beta')
        TrackedRepository.objects.create(user=self.user, owner='aorg', name='alpha')

        repos = list(TrackedRepository.objects.filter(user=self.user))
        self.assertEqual(repos[0].full_name, 'aorg/alpha')
        self.assertEqual(repos[1].full_name, 'aorg/beta')
        self.assertEqual(repos[2].full_name, 'zorg/alpha')
