"""Checks githubstatus.com for degradation of the GitHub API components prdash uses."""
import logging
from dataclasses import dataclass, field

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

STATUS_URL = 'https://www.githubstatus.com/api/v2/components.json'
CACHE_KEY = 'github_status:components'
CACHE_TTL = 60
FETCH_LOCK_KEY = 'github_status:fetch_lock'
FETCH_LOCK_TTL = 6  # slightly above REQUEST_TIMEOUT, just long enough to cover one fetch
REQUEST_TIMEOUT = 5

# Only the components used by the REST Search API and GraphQL calls prdash makes.
TRACKED_COMPONENTS = {'API Requests', 'Pull Requests'}

# githubstatus.com component statuses that indicate degraded availability.
DEGRADED_STATUSES = {'degraded_performance', 'partial_outage', 'major_outage', 'under_maintenance'}


@dataclass
class GitHubStatus:
    """Aggregate health of the GitHub API components prdash depends on."""
    degraded_components: list[str] = field(default_factory=list)
    known: bool = True  # False if the status check itself failed

    @property
    def healthy(self) -> bool:
        return not self.degraded_components


def get_github_status() -> GitHubStatus:
    """Return cached health info for the GitHub components prdash uses."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached

    # Only the first requester past a cache miss fetches; concurrent misses
    # (e.g. many open tabs polling at once) get an "unknown" status instead
    # of each hitting githubstatus.com themselves.
    if not cache.add(FETCH_LOCK_KEY, True, FETCH_LOCK_TTL):
        return GitHubStatus(known=False)

    status = _fetch_github_status()
    cache.set(CACHE_KEY, status, CACHE_TTL)
    return status


def _fetch_github_status() -> GitHubStatus:
    try:
        response = requests.get(STATUS_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        degraded = [
            component.get('name')
            for component in data.get('components', [])
            if component.get('name') in TRACKED_COMPONENTS
            and component.get('status') in DEGRADED_STATUSES
        ]
    except (requests.exceptions.RequestException, ValueError, AttributeError, TypeError) as e:
        logger.warning("Failed to fetch GitHub status: %s", e)
        return GitHubStatus(known=False)

    return GitHubStatus(degraded_components=degraded)
