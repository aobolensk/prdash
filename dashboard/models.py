from django.db import models
from django.contrib.auth.models import User


class PersonalAccessToken(models.Model):
    """A GitHub Personal Access Token for API access.

    Supports both fine-grained tokens (github_pat_...) and classic PATs (ghp_...).
    Fine-grained tokens are recommended as they allow scoping to specific repos.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='github_pat')
    token = models.CharField(max_length=255, help_text="GitHub fine-grained or classic token")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"PAT for {self.user.username}"

    def get_masked_token(self):
        """Return a masked version of the token for display."""
        if len(self.token) > 8:
            return f"{self.token[:4]}...{self.token[-4:]}"
        return "****"


class TrackedRepository(models.Model):
    """A GitHub repository tracked by a user."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='tracked_repos')
    owner = models.CharField(max_length=255, help_text="GitHub username or organization")
    name = models.CharField(max_length=255, help_text="Repository name")
    enabled = models.BooleanField(default=True, help_text="Whether to include in PR fetching")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Tracked repositories"
        unique_together = ['user', 'owner', 'name']
        ordering = ['owner', 'name']

    def __str__(self):
        return f"{self.owner}/{self.name}"

    @property
    def full_name(self):
        return f"{self.owner}/{self.name}"


AUTO_REFRESH_INTERVAL_CHOICES = [
    (1, '1 minute'), (2, '2 minutes'), (5, '5 minutes'), (10, '10 minutes'),
    (15, '15 minutes'), (30, '30 minutes'), (60, '1 hour'),
]

AUTO_REFRESH_CATEGORIES = [
    ('my_prs', 'My PRs'),
    ('review_requests', 'Review Requests'),
    ('assigned', 'Assigned'),
]

PR_LIST_PAGE_SIZE_CHOICES = [
    (25, '25'), (50, '50'), (100, '100'), (0, 'All'),
]


class UserPreferences(models.Model):
    """User preferences for dashboard behavior."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='preferences')
    auto_refresh_my_prs = models.BooleanField(default=False)
    auto_refresh_review_requests = models.BooleanField(default=False)
    auto_refresh_assigned = models.BooleanField(default=False)
    auto_refresh_interval_my_prs = models.PositiveIntegerField(
        default=5, choices=AUTO_REFRESH_INTERVAL_CHOICES
    )
    auto_refresh_interval_review_requests = models.PositiveIntegerField(
        default=5, choices=AUTO_REFRESH_INTERVAL_CHOICES
    )
    auto_refresh_interval_assigned = models.PositiveIntegerField(
        default=5, choices=AUTO_REFRESH_INTERVAL_CHOICES
    )
    pr_list_page_size = models.PositiveIntegerField(
        default=25, choices=PR_LIST_PAGE_SIZE_CHOICES
    )
    show_github_status = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "User preferences"

    def __str__(self):
        return f"Preferences for {self.user.username}"

    TAB_CATEGORIES = {
        'open': 'my_prs',
        'merged': 'my_prs',
        'review_requests': 'review_requests',
        'assigned': 'assigned',
    }

    def is_auto_refresh_enabled_for_tab(self, tab):
        """Check if auto-refresh is enabled for a specific tab."""
        category = self.TAB_CATEGORIES.get(tab)
        if category is None:
            return False
        return getattr(self, f'auto_refresh_{category}')

    def get_auto_refresh_interval_for_tab(self, tab):
        """Return the auto-refresh interval in minutes for a specific tab."""
        category = self.TAB_CATEGORIES.get(tab)
        if category is None:
            return 5
        return getattr(self, f'auto_refresh_interval_{category}')

    def get_auto_refresh_interval_seconds_for_tab(self, tab):
        return self.get_auto_refresh_interval_for_tab(tab) * 60

    def is_github_status_enabled(self):
        """Check if the GitHub API status indicator should be shown."""
        return self.show_github_status

    def get_auto_refresh_rows(self):
        """Return per-tab-category auto-refresh settings for rendering in the preferences form."""
        return [
            {
                'category': category,
                'label': label,
                'enabled': getattr(self, f'auto_refresh_{category}'),
                'interval': getattr(self, f'auto_refresh_interval_{category}'),
                'interval_choices': AUTO_REFRESH_INTERVAL_CHOICES,
            }
            for category, label in AUTO_REFRESH_CATEGORIES
        ]
