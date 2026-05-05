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
