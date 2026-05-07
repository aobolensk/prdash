from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from dashboard.templatetags.dashboard_tags import hours_display, is_light_color, time_ago


class TimeAgoFilterTests(TestCase):
    """Tests for time_ago template filter."""

    def test_time_ago_just_now(self):
        """Verify <60 seconds."""
        now = timezone.now()
        self.assertEqual(time_ago(now), 'just now')

    def test_time_ago_minutes_singular(self):
        """Verify singular minute."""
        one_min_ago = timezone.now() - timedelta(minutes=1)
        self.assertEqual(time_ago(one_min_ago), '1 minute ago')

    def test_time_ago_minutes_plural(self):
        """Verify plural minutes."""
        five_min_ago = timezone.now() - timedelta(minutes=5)
        self.assertEqual(time_ago(five_min_ago), '5 minutes ago')

    def test_time_ago_hours(self):
        """Verify hours display."""
        two_hours_ago = timezone.now() - timedelta(hours=2)
        self.assertEqual(time_ago(two_hours_ago), '2 hours ago')

    def test_time_ago_days(self):
        """Verify days display."""
        three_days_ago = timezone.now() - timedelta(days=3)
        self.assertEqual(time_ago(three_days_ago), '3 days ago')

    def test_time_ago_weeks(self):
        """Verify weeks display."""
        two_weeks_ago = timezone.now() - timedelta(weeks=2)
        self.assertEqual(time_ago(two_weeks_ago), '2 weeks ago')

    def test_time_ago_months(self):
        """Verify months display."""
        two_months_ago = timezone.now() - timedelta(days=60)
        self.assertEqual(time_ago(two_months_ago), '2 months ago')

    def test_time_ago_years(self):
        """Verify years display."""
        two_years_ago = timezone.now() - timedelta(days=730)
        self.assertEqual(time_ago(two_years_ago), '2 years ago')

    def test_time_ago_empty(self):
        """Verify empty/None handling."""
        self.assertEqual(time_ago(None), '')
        self.assertEqual(time_ago(''), '')

    def test_time_ago_string_input(self):
        """Verify string passthrough."""
        self.assertEqual(time_ago('some string'), 'some string')


class IsLightColorFilterTests(TestCase):
    """Tests for is_light_color template filter."""

    def test_is_light_color_white(self):
        """Verify #FFFFFF is light."""
        self.assertTrue(is_light_color('FFFFFF'))
        self.assertTrue(is_light_color('#FFFFFF'))

    def test_is_light_color_black(self):
        """Verify #000000 is not light."""
        self.assertFalse(is_light_color('000000'))
        self.assertFalse(is_light_color('#000000'))

    def test_is_light_color_no_hash(self):
        """Verify works without # prefix."""
        self.assertTrue(is_light_color('FFFF00'))  # Yellow - light

    def test_is_light_color_empty(self):
        """Verify empty returns True."""
        self.assertTrue(is_light_color(''))
        self.assertTrue(is_light_color(None))

    def test_is_light_color_invalid(self):
        """Verify invalid hex returns True."""
        self.assertTrue(is_light_color('invalid'))
        self.assertTrue(is_light_color('ZZZ'))


class HoursDisplayFilterTests(TestCase):
    """Tests for hours_display template filter."""

    def test_hours_display_zero(self):
        """Verify 0 returns '0h'."""
        self.assertEqual(hours_display(0), '0h')
        self.assertEqual(hours_display(None), '0h')

    def test_hours_display_hours_only(self):
        """Verify <24h format."""
        self.assertEqual(hours_display(12), '12h')
        self.assertEqual(hours_display(5.5), '5h')

    def test_hours_display_days_hours(self):
        """Verify >=24h format 'Xd Yh'."""
        self.assertEqual(hours_display(28), '1d 4h')
        self.assertEqual(hours_display(50), '2d 2h')

    def test_hours_display_days_only(self):
        """Verify exact days 'Xd'."""
        self.assertEqual(hours_display(24), '1d')
        self.assertEqual(hours_display(48), '2d')
        self.assertEqual(hours_display(72), '3d')

    def test_hours_display_invalid(self):
        """Verify invalid input handling."""
        self.assertEqual(hours_display('invalid'), '0h')
