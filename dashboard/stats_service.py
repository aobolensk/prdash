"""Stats computation service for PR analytics."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone

from .github_client import GitHubClient, PullRequestInfo


@dataclass
class QuickStats:
    """Quick summary stats."""
    open_count: int = 0
    merged_count: int = 0
    closed_count: int = 0
    avg_merge_time_hours: float = 0.0


@dataclass
class WeeklyData:
    """Data for a single week."""
    week_start: datetime
    opened: int = 0
    merged: int = 0
    closed: int = 0
    lines_added: int = 0
    lines_removed: int = 0


@dataclass
class VelocityStats:
    """PR velocity over time."""
    weekly_data: list[WeeklyData] = field(default_factory=list)
    avg_prs_per_week: float = 0.0
    avg_merge_time_hours: float = 0.0
    total_lines_changed: int = 0


@dataclass
class ReviewerData:
    """Data about a reviewer."""
    username: str
    avatar_url: str = ""
    review_count: int = 0
    avg_turnaround_hours: float = 0.0


@dataclass
class ReviewStats:
    """Review activity stats."""
    reviews_given: int = 0
    reviews_received: int = 0
    avg_turnaround_hours: float = 0.0
    top_reviewers: list[ReviewerData] = field(default_factory=list)
    top_reviewed_by: list[ReviewerData] = field(default_factory=list)


@dataclass
class AgingPR:
    """A PR with age info."""
    pr: PullRequestInfo
    age_days: int = 0


@dataclass
class HealthStats:
    """PR health metrics."""
    aging_7_days: list[AgingPR] = field(default_factory=list)
    aging_14_days: list[AgingPR] = field(default_factory=list)
    aging_30_days: list[AgingPR] = field(default_factory=list)
    failing_ci_prs: list = field(default_factory=list)
    failing_ci_count: int = 0


@dataclass
class RepoData:
    """Stats for a single repo."""
    owner: str
    name: str
    open_count: int = 0
    merged_count: int = 0
    avg_merge_time_hours: float = 0.0
    activity_this_month: int = 0

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class RepoStats:
    """Repository breakdown stats."""
    repos: list[RepoData] = field(default_factory=list)
    total_open: int = 0
    total_merged: int = 0


@dataclass
class CollaboratorData:
    """Collaboration data for a user."""
    username: str
    avatar_url: str = ""
    count: int = 0


@dataclass
class CollaborationStats:
    """Collaboration patterns."""
    who_reviews_you: list[CollaboratorData] = field(default_factory=list)
    who_you_review: list[CollaboratorData] = field(default_factory=list)


class StatsService:
    """Service for computing PR statistics."""

    CACHE_TTL = 300  # 5 minutes

    def __init__(self, client: GitHubClient):
        self.client = client
        self.username = client.get_username()
        self._pr_cache: dict[str, list[PullRequestInfo]] = {}

    def _get_cache_key(self, prefix: str, repos: list[tuple[str, str]], days: int) -> str:
        """Generate cache key for stats."""
        repos_str = ",".join(f"{o}/{n}" for o, n in sorted(repos))
        return f"stats:{prefix}:{self.username}:{repos_str}:{days}"

    def _get_week_start(self, dt: datetime) -> datetime:
        """Get the Monday of the week for a date."""
        days_since_monday = dt.weekday()
        return (dt - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    def get_prs_for_stats(
        self,
        repos: list[tuple[str, str]],
        days: int,
        include_closed: bool = True
    ) -> list[PullRequestInfo]:
        """Get PRs for stats computation with caching."""
        cache_key = f"prs_stats:{self.username}:{days}:{include_closed}"
        if cache_key in self._pr_cache:
            return self._pr_cache[cache_key]

        all_prs = []
        cutoff_date = timezone.now() - timedelta(days=days)

        # Get open PRs
        open_prs = self.client.get_all_user_prs(repos)
        all_prs.extend(open_prs)

        # Get merged PRs
        merged_prs = self.client.get_all_merged_prs(repos)
        # Filter to date range
        merged_prs = [pr for pr in merged_prs if pr.merged_at and pr.merged_at >= cutoff_date]
        all_prs.extend(merged_prs)

        self._pr_cache[cache_key] = all_prs
        return all_prs

    def get_quick_stats(self, repos: list[tuple[str, str]], days: int = 30) -> QuickStats:
        """Get quick summary statistics."""
        cache_key = self._get_cache_key("quick", repos, days)
        cached = cache.get(cache_key)
        if cached:
            return cached

        prs = self.get_prs_for_stats(repos, days)
        cutoff_date = timezone.now() - timedelta(days=days)

        open_count = 0
        merged_count = 0
        closed_count = 0
        total_merge_time = timedelta()
        merge_time_count = 0

        for pr in prs:
            if pr.merged_at:
                if pr.merged_at >= cutoff_date:
                    merged_count += 1
                    merge_time = pr.merged_at - pr.created_at
                    total_merge_time += merge_time
                    merge_time_count += 1
            elif pr.created_at >= cutoff_date:
                open_count += 1

        avg_merge_hours = 0.0
        if merge_time_count > 0:
            avg_merge_hours = total_merge_time.total_seconds() / 3600 / merge_time_count

        stats = QuickStats(
            open_count=open_count,
            merged_count=merged_count,
            closed_count=closed_count,
            avg_merge_time_hours=avg_merge_hours
        )
        cache.set(cache_key, stats, self.CACHE_TTL)
        return stats

    def get_velocity_stats(self, repos: list[tuple[str, str]], days: int = 90) -> VelocityStats:
        """Get PR velocity statistics over time."""
        cache_key = self._get_cache_key("velocity", repos, days)
        cached = cache.get(cache_key)
        if cached:
            return cached

        prs = self.get_prs_for_stats(repos, days)
        now = timezone.now()
        cutoff_date = now - timedelta(days=days)

        # Initialize weekly buckets
        weeks: dict[datetime, WeeklyData] = {}
        current = self._get_week_start(cutoff_date)
        while current <= now:
            weeks[current] = WeeklyData(week_start=current)
            current += timedelta(days=7)

        total_merge_time = timedelta()
        merge_count = 0
        total_lines = 0

        for pr in prs:
            # Count opened PRs
            if pr.created_at >= cutoff_date:
                week_start = self._get_week_start(pr.created_at)
                if week_start in weeks:
                    weeks[week_start].opened += 1
                    weeks[week_start].lines_added += pr.additions
                    weeks[week_start].lines_removed += pr.deletions
                    total_lines += pr.additions + pr.deletions

            # Count merged PRs
            if pr.merged_at and pr.merged_at >= cutoff_date:
                week_start = self._get_week_start(pr.merged_at)
                if week_start in weeks:
                    weeks[week_start].merged += 1
                merge_time = pr.merged_at - pr.created_at
                total_merge_time += merge_time
                merge_count += 1

        weekly_data = sorted(weeks.values(), key=lambda w: w.week_start)
        num_weeks = len(weekly_data) or 1
        total_prs = sum(w.opened for w in weekly_data)
        avg_prs = total_prs / num_weeks

        avg_merge_hours = 0.0
        if merge_count > 0:
            avg_merge_hours = total_merge_time.total_seconds() / 3600 / merge_count

        stats = VelocityStats(
            weekly_data=weekly_data,
            avg_prs_per_week=avg_prs,
            avg_merge_time_hours=avg_merge_hours,
            total_lines_changed=total_lines
        )
        cache.set(cache_key, stats, self.CACHE_TTL)
        return stats

    def get_review_stats(self, repos: list[tuple[str, str]], days: int = 30) -> ReviewStats:
        """Get review activity statistics."""
        cache_key = self._get_cache_key("reviews", repos, days)
        cached = cache.get(cache_key)
        if cached:
            return cached

        # Get reviews data from GitHub
        reviews_data = self.client.get_reviews_for_stats(repos, self.username, days)

        reviews_given = reviews_data.get('reviews_given', 0)
        reviews_received = reviews_data.get('reviews_received', 0)
        avg_turnaround = reviews_data.get('avg_turnaround_hours', 0.0)

        top_reviewers = [
            ReviewerData(
                username=r['username'],
                avatar_url=r.get('avatar_url', ''),
                review_count=r['count']
            )
            for r in reviews_data.get('top_reviewers', [])[:5]
        ]

        top_reviewed_by = [
            ReviewerData(
                username=r['username'],
                avatar_url=r.get('avatar_url', ''),
                review_count=r['count']
            )
            for r in reviews_data.get('top_reviewed_by', [])[:5]
        ]

        stats = ReviewStats(
            reviews_given=reviews_given,
            reviews_received=reviews_received,
            avg_turnaround_hours=avg_turnaround,
            top_reviewers=top_reviewers,
            top_reviewed_by=top_reviewed_by
        )
        cache.set(cache_key, stats, self.CACHE_TTL)
        return stats

    def get_health_stats(self, repos: list[tuple[str, str]]) -> HealthStats:
        """Get PR health statistics."""
        cache_key = self._get_cache_key("health", repos, 0)
        cached = cache.get(cache_key)
        if cached:
            return cached

        open_prs = self.client.get_all_user_prs(repos)
        now = timezone.now()

        aging_7: list[AgingPR] = []
        aging_14: list[AgingPR] = []
        aging_30: list[AgingPR] = []
        failing_ci_prs = []

        for pr in open_prs:
            age = now - pr.created_at
            age_days = age.days
            aging_pr = AgingPR(pr=pr, age_days=age_days)

            if age_days >= 30:
                aging_30.append(aging_pr)
            elif age_days >= 14:
                aging_14.append(aging_pr)
            elif age_days >= 7:
                aging_7.append(aging_pr)

            # Collect failing CI PRs
            if pr.ci_status.state in ('failure', 'error'):
                failing_ci_prs.append(pr)

        # Sort by age descending
        aging_7.sort(key=lambda x: x.age_days, reverse=True)
        aging_14.sort(key=lambda x: x.age_days, reverse=True)
        aging_30.sort(key=lambda x: x.age_days, reverse=True)

        stats = HealthStats(
            aging_7_days=aging_7[:10],
            aging_14_days=aging_14[:10],
            aging_30_days=aging_30[:10],
            failing_ci_prs=failing_ci_prs[:10],
            failing_ci_count=len(failing_ci_prs)
        )
        cache.set(cache_key, stats, self.CACHE_TTL)
        return stats

    def get_repo_stats(self, repos: list[tuple[str, str]], days: int = 30) -> RepoStats:
        """Get per-repository statistics."""
        cache_key = self._get_cache_key("repos", repos, days)
        cached = cache.get(cache_key)
        if cached:
            return cached

        prs = self.get_prs_for_stats(repos, days)
        now = timezone.now()
        cutoff_date = now - timedelta(days=days)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        repo_data: dict[tuple[str, str], RepoData] = {}
        for owner, name in repos:
            repo_data[(owner, name)] = RepoData(owner=owner, name=name)

        for pr in prs:
            key = (pr.repo_owner, pr.repo_name)
            if key not in repo_data:
                repo_data[key] = RepoData(owner=pr.repo_owner, name=pr.repo_name)

            rd = repo_data[key]
            if pr.merged_at:
                if pr.merged_at >= cutoff_date:
                    rd.merged_count += 1
                if pr.merged_at >= month_start:
                    rd.activity_this_month += 1
            else:
                rd.open_count += 1
                if pr.created_at >= month_start:
                    rd.activity_this_month += 1

        repos_list = sorted(
            repo_data.values(),
            key=lambda r: r.open_count + r.merged_count,
            reverse=True
        )

        total_open = sum(r.open_count for r in repos_list)
        total_merged = sum(r.merged_count for r in repos_list)

        stats = RepoStats(
            repos=repos_list,
            total_open=total_open,
            total_merged=total_merged
        )
        cache.set(cache_key, stats, self.CACHE_TTL)
        return stats

    def get_collaboration_stats(self, repos: list[tuple[str, str]], days: int = 30) -> CollaborationStats:
        """Get collaboration statistics."""
        cache_key = self._get_cache_key("collab", repos, days)
        cached = cache.get(cache_key)
        if cached:
            return cached

        reviews_data = self.client.get_reviews_for_stats(repos, self.username, days)

        who_reviews_you = [
            CollaboratorData(
                username=r['username'],
                avatar_url=r.get('avatar_url', ''),
                count=r['count']
            )
            for r in reviews_data.get('top_reviewed_by', [])[:10]
        ]

        who_you_review = [
            CollaboratorData(
                username=r['username'],
                avatar_url=r.get('avatar_url', ''),
                count=r['count']
            )
            for r in reviews_data.get('top_reviewers', [])[:10]
        ]

        stats = CollaborationStats(
            who_reviews_you=who_reviews_you,
            who_you_review=who_you_review
        )
        cache.set(cache_key, stats, self.CACHE_TTL)
        return stats

    def get_all_stats(self, repos: list[tuple[str, str]], days: int = 30) -> dict:
        """Get all stats in one call."""
        return {
            'quick': self.get_quick_stats(repos, days),
            'velocity': self.get_velocity_stats(repos, days),
            'reviews': self.get_review_stats(repos, days),
            'health': self.get_health_stats(repos),
            'repos': self.get_repo_stats(repos, days),
            'collaboration': self.get_collaboration_stats(repos, days),
        }
