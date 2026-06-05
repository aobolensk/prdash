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


class UserPreferences(models.Model):
    """User preferences for dashboard behavior."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='preferences')
    auto_refresh_my_prs = models.BooleanField(default=False)
    auto_refresh_review_requests = models.BooleanField(default=False)
    auto_refresh_assigned = models.BooleanField(default=False)
    auto_refresh_interval = models.PositiveIntegerField(
        default=5,
        choices=[(1, '1 minute'), (2, '2 minutes'), (5, '5 minutes'), (10, '10 minutes')]
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "User preferences"

    def __str__(self):
        return f"Preferences for {self.user.username}"

    @property
    def auto_refresh_interval_seconds(self):
        return self.auto_refresh_interval * 60

    def is_auto_refresh_enabled_for_tab(self, tab):
        """Check if auto-refresh is enabled for a specific tab."""
        tab_map = {
            'open': self.auto_refresh_my_prs,
            'merged': self.auto_refresh_my_prs,
            'review_requests': self.auto_refresh_review_requests,
            'assigned': self.auto_refresh_assigned,
        }
        return tab_map.get(tab, False)
