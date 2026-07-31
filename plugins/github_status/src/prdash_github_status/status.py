"""Checks githubstatus.com for degradation of GitHub API components."""

from dataclasses import dataclass, field
import logging

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

STATUS_URL = 'https://www.githubstatus.com/api/v2/components.json'
CACHE_KEY = 'github_status:components'
CACHE_TTL = 60
FETCH_LOCK_KEY = 'github_status:fetch_lock'
FETCH_LOCK_TTL = 6
REQUEST_TIMEOUT = 5

TRACKED_COMPONENTS = {'API Requests', 'Pull Requests'}
DEGRADED_STATUSES = {
    'degraded_performance',
    'partial_outage',
    'major_outage',
    'under_maintenance',
}


@dataclass
class GitHubStatus:
    """Aggregate health of the GitHub API components used by prdash."""

    degraded_components: list[str] = field(default_factory=list)
    known: bool = True

    @property
    def healthy(self):
        return not self.degraded_components


def get_github_status():
    """Return cached health information for tracked GitHub components."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached

    if not cache.add(FETCH_LOCK_KEY, True, FETCH_LOCK_TTL):
        return GitHubStatus(known=False)

    status = _fetch_github_status()
    cache.set(CACHE_KEY, status, CACHE_TTL)
    return status


def _fetch_github_status():
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
    except (
        requests.exceptions.RequestException,
        ValueError,
        AttributeError,
        TypeError,
    ) as error:
        logger.warning("Failed to fetch GitHub status: %s", error)
        return GitHubStatus(known=False)

    return GitHubStatus(degraded_components=degraded)
