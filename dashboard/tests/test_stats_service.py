from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from dashboard.github_client import CIStatus, PullRequestInfo, ReviewStatus
from dashboard.stats_service import StatsService


class StatsServiceQuickStatsTests(TestCase):
    """Tests for QuickStats computation."""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')

    def _create_mock_client(self, open_prs=None, merged_prs=None):
        mock_client = MagicMock()
        mock_client.get_username.return_value = 'testuser'
        mock_client.get_all_user_prs.return_value = open_prs or []
        mock_client.get_all_merged_prs.return_value = merged_prs or []
        return mock_client

    def _create_pr(self, number, created_days_ago, merged_days_ago=None):
        now = timezone.now()
        created_at = now - timedelta(days=created_days_ago)
        merged_at = (now - timedelta(days=merged_days_ago)) if merged_days_ago is not None else None

        return PullRequestInfo(
            number=number,
            title=f'PR {number}',
            url=f'https://github.com/owner/repo/pull/{number}',
            repo_owner='owner',
            repo_name='repo',
            author='testuser',
            author_avatar='',
            created_at=created_at,
            updated_at=created_at,
            labels=[],
            ci_status=CIStatus(state='success'),
            review_status=ReviewStatus(state='approved'),
            draft=False,
            additions=10,
            deletions=5,
            merged_at=merged_at,
        )

    @patch('dashboard.stats_service.cache')
    def test_quick_stats_counts(self, mock_cache):
        """Verify open/merged counts."""
        mock_cache.get.return_value = None

        open_prs = [self._create_pr(1, 5), self._create_pr(2, 10)]
        merged_prs = [self._create_pr(3, 15, merged_days_ago=5)]

        mock_client = self._create_mock_client(open_prs, merged_prs)
        service = StatsService(mock_client)

        stats = service.get_quick_stats([('owner', 'repo')], days=30)

        self.assertEqual(stats.open_count, 2)
        self.assertEqual(stats.merged_count, 1)

    @patch('dashboard.stats_service.cache')
    def test_quick_stats_empty_repos(self, mock_cache):
        """Verify handling of no repos."""
        mock_cache.get.return_value = None

        mock_client = self._create_mock_client()
        service = StatsService(mock_client)

        stats = service.get_quick_stats([], days=30)

        self.assertEqual(stats.open_count, 0)
        self.assertEqual(stats.merged_count, 0)


class StatsServiceVelocityTests(TestCase):
    """Tests for VelocityStats computation."""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')

    def _create_mock_client(self, prs=None):
        mock_client = MagicMock()
        mock_client.get_username.return_value = 'testuser'
        mock_client.get_all_user_prs.return_value = prs or []
        mock_client.get_all_merged_prs.return_value = []
        return mock_client

    @patch('dashboard.stats_service.cache')
    def test_velocity_stats_daily_granularity(self, mock_cache):
        """Verify daily granularity for <=14 days."""
        mock_cache.get.return_value = None

        mock_client = self._create_mock_client()
        service = StatsService(mock_client)

        stats = service.get_velocity_stats([('owner', 'repo')], days=7)

        self.assertEqual(stats.granularity, 'day')

    @patch('dashboard.stats_service.cache')
    def test_velocity_stats_weekly_granularity(self, mock_cache):
        """Verify weekly granularity for >14 days."""
        mock_cache.get.return_value = None

        mock_client = self._create_mock_client()
        service = StatsService(mock_client)

        stats = service.get_velocity_stats([('owner', 'repo')], days=30)

        self.assertEqual(stats.granularity, 'week')


class StatsServiceHealthTests(TestCase):
    """Tests for HealthStats computation."""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')

    def _create_pr(self, number, created_days_ago, ci_state='success'):
        now = timezone.now()
        created_at = now - timedelta(days=created_days_ago)

        return PullRequestInfo(
            number=number,
            title=f'PR {number}',
            url=f'https://github.com/owner/repo/pull/{number}',
            repo_owner='owner',
            repo_name='repo',
            author='testuser',
            author_avatar='',
            created_at=created_at,
            updated_at=created_at,
            labels=[],
            ci_status=CIStatus(state=ci_state),
            review_status=ReviewStatus(state='not_reviewed'),
            draft=False,
            additions=10,
            deletions=5,
        )

    @patch('dashboard.stats_service.cache')
    def test_health_stats_aging_buckets(self, mock_cache):
        """Verify 7/14/30 day buckets."""
        mock_cache.get.return_value = None

        prs = [
            self._create_pr(1, 5),    # < 7 days, not aged
            self._create_pr(2, 8),    # 7-13 days
            self._create_pr(3, 20),   # 14-29 days
            self._create_pr(4, 35),   # 30+ days
        ]

        mock_client = MagicMock()
        mock_client.get_username.return_value = 'testuser'
        mock_client.get_all_user_prs.return_value = prs
        service = StatsService(mock_client)

        stats = service.get_health_stats([('owner', 'repo')])

        self.assertEqual(len(stats.aging_7_days), 1)
        self.assertEqual(len(stats.aging_14_days), 1)
        self.assertEqual(len(stats.aging_30_days), 1)

    @patch('dashboard.stats_service.cache')
    def test_health_stats_failing_ci_collection(self, mock_cache):
        """Verify CI failure tracking."""
        mock_cache.get.return_value = None

        prs = [
            self._create_pr(1, 5, ci_state='success'),
            self._create_pr(2, 5, ci_state='failure'),
            self._create_pr(3, 5, ci_state='error'),
        ]

        mock_client = MagicMock()
        mock_client.get_username.return_value = 'testuser'
        mock_client.get_all_user_prs.return_value = prs
        service = StatsService(mock_client)

        stats = service.get_health_stats([('owner', 'repo')])

        self.assertEqual(stats.failing_ci_count, 2)


class StatsServiceCachingTests(TestCase):
    """Tests for StatsService caching."""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')

    def test_cache_key_generation(self):
        """Verify unique keys per params."""
        mock_client = MagicMock()
        mock_client.get_username.return_value = 'testuser'
        service = StatsService(mock_client)

        key1 = service._get_cache_key('quick', [('org', 'repo')], 30)
        key2 = service._get_cache_key('quick', [('org', 'repo')], 90)
        key3 = service._get_cache_key('velocity', [('org', 'repo')], 30)

        self.assertNotEqual(key1, key2)
        self.assertNotEqual(key1, key3)
