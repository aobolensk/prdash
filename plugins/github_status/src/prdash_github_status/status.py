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

WARNING_STATUSES = {'degraded_performance', 'under_maintenance'}
OUTAGE_STATUSES = {'partial_outage', 'major_outage'}
STATUS_LABELS = {
    'degraded_performance': 'degraded performance',
    'under_maintenance': 'under maintenance',
    'partial_outage': 'partial outage',
    'major_outage': 'major outage',
}


@dataclass
class ComponentStatus:
    name: str
    status: str

    @property
    def label(self):
        return STATUS_LABELS.get(self.status, self.status)

    def __str__(self):
        return f'{self.name} {self.label}'


@dataclass
class GitHubStatus:
    """Aggregate health of the GitHub API components used by prdash."""

    warning_components: list[ComponentStatus] = field(default_factory=list)
    outage_components: list[ComponentStatus] = field(default_factory=list)
    known: bool = True

    @property
    def healthy(self):
        return not self.warning_components and not self.outage_components

    @property
    def outage(self):
        return bool(self.outage_components)

    @property
    def degraded_components(self):
        return self.warning_components + self.outage_components


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
        tracked = [
            component
            for component in data.get('components', [])
            if component.get('name') in TRACKED_COMPONENTS
        ]
        warning = [
            ComponentStatus(c.get('name'), c.get('status'))
            for c in tracked
            if c.get('status') in WARNING_STATUSES
        ]
        outage = [
            ComponentStatus(c.get('name'), c.get('status'))
            for c in tracked
            if c.get('status') in OUTAGE_STATUSES
        ]
    except (
        requests.exceptions.RequestException,
        ValueError,
        AttributeError,
        TypeError,
    ) as error:
        logger.warning("Failed to fetch GitHub status: %s", error)
        return GitHubStatus(known=False)

    return GitHubStatus(warning_components=warning, outage_components=outage)
