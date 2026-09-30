from pathlib import Path
import sys
from unittest.mock import patch

from django.template import engines
from django.test import SimpleTestCase


SRC_DIR = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC_DIR))

from prdash_stats.stats_service import (  # noqa: E402
    HealthStats,
    QuickStats,
    RepoStats,
    StatsService,
    VelocityStats,
)


class FailingReviewRegistrar:
    def get_username(self, user_id):
        return 'test-user'

    def fetch_open_prs(self, user_id, repos):
        return []

    def fetch_merged_prs(self, user_id, repos):
        return []

    def fetch_reviews_for_stats(self, user_id, repos, username, days):
        raise RuntimeError('review fetch failed')


class StatsServiceTests(SimpleTestCase):
    @patch('prdash_stats.stats_service._cache_set')
    @patch('prdash_stats.stats_service._cache_get', return_value=None)
    def test_review_fetch_failure_is_reported_without_zero_stats(self, _cache_get, _cache_set):
        service = StatsService(FailingReviewRegistrar(), user_id=1)

        results = service.get_all_stats([('owner', 'repo')])

        self.assertIsNone(results['reviews'].data)
        self.assertEqual(results['reviews'].error, 'review fetch failed')
        self.assertIsNone(results['collaboration'].data)
        self.assertEqual(results['collaboration'].error, 'review fetch failed')
        self.assertIsInstance(results['quick'].data, QuickStats)
        self.assertIsNone(results['quick'].error)


class StatsTemplateTests(SimpleTestCase):
    def test_failed_sections_are_rendered_without_zero_like_values(self):
        source = (
            Path(__file__).resolve().parents[1]
            / 'src'
            / 'prdash_stats'
            / 'templates'
            / 'content.html'
        ).read_text()
        template = engines['django'].from_string(source)
        rendered = template.render({
            'quick_stats': QuickStats(open_count=2),
            'quick_stats_error': None,
            'velocity_stats': VelocityStats(),
            'velocity_stats_error': None,
            'review_stats': None,
            'review_stats_error': 'review fetch failed',
            'health_stats': HealthStats(),
            'health_stats_error': None,
            'repo_stats': RepoStats(),
            'repo_stats_error': None,
            'collaboration_stats': None,
            'collaboration_stats_error': 'review fetch failed',
        })

        self.assertIn('Open PRs', rendered)
        self.assertIn('2', rendered)
        self.assertIn('Review activity unavailable: review fetch failed', rendered)
        self.assertIn('Collaboration stats unavailable: review fetch failed', rendered)
        self.assertNotIn('Reviews Given', rendered)
        self.assertNotIn('No review data available', rendered)
