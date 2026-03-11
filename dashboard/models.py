from django.db import models
from django.contrib.auth.models import User


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
