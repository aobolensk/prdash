"""Read-only GitHub API access for guest sessions."""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import quote

import requests
from django.core.cache import cache
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .github_client import CIStatus, PullRequestInfo, ReviewStatus
from prdash.plugin_api import GITHUB_API_VERSION


logger = logging.getLogger(__name__)

GITHUB_REST_API = 'https://api.github.com'
GUEST_PR_CACHE_SECONDS = 60
GUEST_ERROR_CACHE_SECONDS = 15
GUEST_PR_PAGE_SIZE = 50
GUEST_PR_DETAILS_CACHE_SECONDS = 600
GUEST_CI_CACHE_SECONDS = 300
_GUEST_API_UNAVAILABLE = {'_guest_api_unavailable': None}


@dataclass(slots=True)
class GuestPullRequestInfo(PullRequestInfo):
    """Public pull request details that need one extra REST request to load."""
    head_sha: str = ''
    has_diff_stats: bool = False


def _request_public_api(url, **kwargs):
    app = settings.SOCIALACCOUNT_PROVIDERS.get('github', {}).get('APP', {})
    client_id = app.get('client_id', '')
    client_secret = app.get('secret', '')
    auth = (client_id, client_secret) if client_id and client_secret and not client_id.startswith('your_') else None
    kwargs.setdefault('headers', {
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': GITHUB_API_VERSION,
        'User-Agent': 'PR-Dashboard-Guest-View',
    })
    kwargs.setdefault('timeout', (3, 8))
    kwargs['auth'] = auth
    response = requests.get(url, **kwargs)
    if response.status_code == 401 and auth:
        kwargs['auth'] = None
        response = requests.get(url, **kwargs)
    return response


def fetch_public_open_pull_requests(owner, name, page=1):
    """Fetch one page of public open PRs for a guest session."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', owner or ''):
        return [], False, 'Enter a valid public GitHub repository.'
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name or '') or name in {'.', '..'}:
        return [], False, 'Enter a valid public GitHub repository.'
    try:
        page = min(max(int(page), 1), 10)
    except (TypeError, ValueError):
        page = 1

    cache_key = f'guest_public_prs:{owner.casefold()}/{name.casefold()}:{page}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached['pull_requests'], cached['has_next'], cached['error']

    url = f'{GITHUB_REST_API}/repos/{quote(owner, safe="")}/{quote(name, safe="")}/pulls'
    try:
        response = _request_public_api(
            url,
            params={
                'state': 'open',
                'sort': 'updated',
                'direction': 'desc',
                'per_page': GUEST_PR_PAGE_SIZE,
                'page': page,
            },
        )
    except requests.RequestException:
        logger.info('Guest GitHub request failed for %s/%s', owner, name)
        return [], False, 'GitHub is temporarily unavailable. Try again shortly.'

    if response.status_code == 404:
        result = {
            'pull_requests': [],
            'has_next': False,
            'error': 'This repository is private or could not be found.',
        }
        cache.set(cache_key, result, GUEST_ERROR_CACHE_SECONDS)
        return result['pull_requests'], result['has_next'], result['error']
    if response.status_code in {403, 429}:
        result = ([], False, 'GitHub is limiting guest requests. Try again later or sign in.')
        cache.set(
            cache_key,
            {'pull_requests': result[0], 'has_next': result[1], 'error': result[2]},
            GUEST_ERROR_CACHE_SECONDS,
        )
        return result
    if response.status_code != 200:
        logger.info('Guest GitHub request returned HTTP %s for %s/%s', response.status_code, owner, name)
        return [], False, 'Could not load pull requests from GitHub. Try again shortly.'

    try:
        payload = response.json()
    except ValueError:
        return [], False, 'GitHub returned an unreadable response. Try again shortly.'
    if not isinstance(payload, list):
        return [], False, 'GitHub returned an unreadable response. Try again shortly.'

    pull_requests = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get('number'), int):
            continue
        author = item.get('user') if isinstance(item.get('user'), dict) else {}
        labels = item.get('labels') if isinstance(item.get('labels'), list) else []
        head = item.get('head') if isinstance(item.get('head'), dict) else {}
        head_repo = head.get('repo') if isinstance(head.get('repo'), dict) else {}
        head_repo_owner = head_repo.get('owner') if isinstance(head_repo.get('owner'), dict) else {}
        updated_at = parse_datetime(item.get('updated_at') or '')
        created_at = parse_datetime(item.get('created_at') or '')
        updated_at = updated_at or created_at or timezone.now()
        created_at = created_at or updated_at
        pull_requests.append({
            'number': item['number'],
            'title': str(item.get('title') or ''),
            'url': f'https://github.com/{owner}/{name}/pull/{item["number"]}',
            'author': str(author.get('login') or 'Unknown'),
            'author_avatar': str(author.get('avatar_url') or ''),
            'draft': bool(item.get('draft')),
            'branch_name': str(head.get('ref') or ''),
            'head_sha': str(head.get('sha') or ''),
            'head_repo_owner': str(head_repo_owner.get('login') or owner),
            'head_repo_name': str(head_repo.get('name') or name),
            'created_at': created_at,
            'updated_at': updated_at,
            'labels': [
                {
                    'name': str(label.get('name') or ''),
                    'color': label.get('color') if re.fullmatch(r'[0-9a-fA-F]{6}', str(label.get('color') or ''))
                    else 'd0d7de',
                }
                for label in labels if isinstance(label, dict)
            ],
        })

    result = {
        'pull_requests': pull_requests,
        'has_next': 'next' in response.links and page < 10,
        'error': None,
    }
    cache.set(cache_key, result, GUEST_PR_CACHE_SECONDS)
    return result['pull_requests'], result['has_next'], result['error']


def fetch_guest_dashboard_pull_requests(repositories, page=1, page_size=25):
    """Fetch and merge public open PRs for a guest dashboard page."""
    try:
        page = max(int(page), 1)
    except (TypeError, ValueError):
        page = 1

    remote_page = min(max((page * page_size + GUEST_PR_PAGE_SIZE - 1) // GUEST_PR_PAGE_SIZE, 1), 10)
    fetched_pages = {}
    if repositories:
        with ThreadPoolExecutor(max_workers=min(8, len(repositories))) as executor:
            repositories_to_fetch = repositories
            for remote_page_number in range(1, remote_page + 1):
                futures = {
                    executor.submit(
                        fetch_public_open_pull_requests,
                        repository['owner'],
                        repository['name'],
                        remote_page_number,
                    ): repository
                    for repository in repositories_to_fetch
                }
                for future in as_completed(futures):
                    repository = futures[future]
                    try:
                        result = future.result()
                    except Exception:
                        logger.exception(
                            'Guest PR fetch failed for %s/%s page %s',
                            repository['owner'], repository['name'], remote_page_number,
                        )
                        result = ([], False, 'Could not load pull requests from GitHub. Try again shortly.')
                    fetched_pages[(repository['owner'], repository['name'], remote_page_number)] = result

                repositories_to_fetch = [
                    repository for repository in repositories_to_fetch
                    if fetched_pages[(repository['owner'], repository['name'], remote_page_number)][1]
                ]
                if not repositories_to_fetch:
                    break

    pull_requests = []
    errors = []
    has_next = False
    for repository in repositories:
        for remote_page_number in range(1, remote_page + 1):
            rows, page_has_next, error = fetched_pages.get(
                (repository['owner'], repository['name'], remote_page_number),
                ([], False, None),
            )
            if remote_page_number == remote_page:
                has_next = has_next or page_has_next
            if error and error not in errors:
                errors.append(error)
            pull_requests.extend(
                GuestPullRequestInfo(
                    number=row['number'],
                    title=row['title'],
                    url=row['url'],
                    repo_owner=repository['owner'],
                    repo_name=repository['name'],
                    author=row['author'],
                    author_avatar=row.get('author_avatar', ''),
                    created_at=row['created_at'],
                    updated_at=row['updated_at'],
                    labels=row['labels'],
                    ci_status=CIStatus(state='unknown'),
                    review_status=ReviewStatus(state='not_reviewed'),
                    draft=row['draft'],
                    additions=0,
                    deletions=0,
                    branch_name=row['branch_name'],
                    head_repo_owner=row['head_repo_owner'],
                    head_repo_name=row['head_repo_name'],
                    head_sha=row['head_sha'],
                )
                for row in rows
            )

    pull_requests.sort(
        key=lambda pr: (
            pr.updated_at.timestamp() if pr.updated_at else 0,
            pr.repo_full_name.casefold(),
            pr.number,
        ),
        reverse=True,
    )
    return pull_requests, has_next, '; '.join(errors) if errors else None


def _cached_public_json(url, cache_key, cache_seconds):
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        response = _request_public_api(url)
    except requests.RequestException:
        cache.set(cache_key, _GUEST_API_UNAVAILABLE, GUEST_ERROR_CACHE_SECONDS)
        return _GUEST_API_UNAVAILABLE
    if response.status_code != 200:
        unavailable = {'_guest_api_unavailable': response.status_code}
        cache.set(cache_key, unavailable, GUEST_ERROR_CACHE_SECONDS)
        return unavailable
    try:
        payload = response.json()
    except ValueError:
        cache.set(cache_key, _GUEST_API_UNAVAILABLE, GUEST_ERROR_CACHE_SECONDS)
        return _GUEST_API_UNAVAILABLE
    cache.set(cache_key, payload, cache_seconds)
    return payload


def _fetch_public_diff_stats(pr):
    cache_key = (
        f'guest_pr_details:{pr.repo_full_name.casefold()}:{pr.number}:'
        f'{pr.updated_at.isoformat() if pr.updated_at else ""}'
    )
    payload = _cached_public_json(
        f'{GITHUB_REST_API}/repos/{quote(pr.repo_owner, safe="")}/'
        f'{quote(pr.repo_name, safe="")}/pulls/{pr.number}',
        cache_key,
        GUEST_PR_DETAILS_CACHE_SECONDS,
    )
    if not isinstance(payload, dict):
        return None
    additions = payload.get('additions')
    deletions = payload.get('deletions')
    if not isinstance(additions, int) or not isinstance(deletions, int):
        return None
    auto_merge = payload.get('auto_merge') if isinstance(payload.get('auto_merge'), dict) else {}
    enabled_by = auto_merge.get('enabled_by') if isinstance(auto_merge.get('enabled_by'), dict) else {}
    return {
        'additions': additions,
        'deletions': deletions,
        'mergeable': 'CONFLICTING' if payload.get('mergeable') is False else None,
        'auto_merge_enabled': bool(auto_merge),
        'auto_merge_enabled_by': enabled_by.get('login'),
    }


def _parse_public_check_runs(payload):
    check_runs = payload.get('check_runs', [])
    if not isinstance(check_runs, list):
        return CIStatus(state='unknown')

    success_count = 0
    failure_count = 0
    pending_count = 0
    skipped_count = 0
    pending_approval = False
    for check_run in check_runs:
        if not isinstance(check_run, dict):
            continue
        conclusion = check_run.get('conclusion')
        if check_run.get('status') != 'completed':
            pending_count += 1
        elif conclusion == 'success':
            success_count += 1
        elif conclusion in {'failure', 'cancelled', 'timed_out', 'startup_failure'}:
            failure_count += 1
        elif conclusion == 'action_required':
            failure_count += 1
            pending_approval = True
        elif conclusion in {'skipped', 'neutral', 'stale'}:
            skipped_count += 1

    total_count = payload.get('total_count', len(check_runs))
    total_count = total_count if isinstance(total_count, int) else len(check_runs)
    if failure_count:
        state = 'failure'
    elif pending_count:
        state = 'pending'
    elif success_count or (total_count and skipped_count == total_count):
        state = 'success'
    else:
        state = 'unknown'
    return CIStatus(
        state=state,
        passed_count=success_count,
        total_count=total_count,
        pending_approval=pending_approval,
    )


def _fetch_public_ci_status(pr):
    if not pr.head_sha:
        return CIStatus(state='unknown')
    cache_key = (
        f'guest_ci:{pr.head_repo_owner.casefold()}/{pr.head_repo_name.casefold()}:'
        f'{pr.head_sha}'
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    checks_url = (
        f'{GITHUB_REST_API}/repos/{quote(pr.head_repo_owner, safe="")}/'
        f'{quote(pr.head_repo_name, safe="")}/commits/{quote(pr.head_sha, safe="")}/check-runs'
    )
    checks = _cached_public_json(
        checks_url + '?filter=latest&per_page=100',
        f'{cache_key}:checks',
        GUEST_CI_CACHE_SECONDS,
    )
    is_unavailable = isinstance(checks, dict) and '_guest_api_unavailable' in checks
    unavailable = checks.get('_guest_api_unavailable') if is_unavailable else None
    if is_unavailable:
        check_status = CIStatus(state='unknown')
        use_legacy_status = unavailable not in {None, 403, 429}
    else:
        check_status = (
            _parse_public_check_runs(checks)
            if isinstance(checks, dict) and checks.get('total_count', 0)
            else CIStatus(state='unknown')
        )
        use_legacy_status = True

    if use_legacy_status:
        status_url = (
            f'{GITHUB_REST_API}/repos/{quote(pr.repo_owner, safe="")}/'
            f'{quote(pr.repo_name, safe="")}/commits/{quote(pr.head_sha, safe="")}/status'
        )
        legacy_status = _cached_public_json(
            status_url,
            f'{cache_key}:status',
            GUEST_CI_CACHE_SECONDS,
        )
        states = legacy_status.get('statuses', []) if isinstance(legacy_status, dict) else []
        states = [check for check in states if isinstance(check, dict)]
        legacy_state = legacy_status.get('state', 'unknown') if isinstance(legacy_status, dict) else 'unknown'
        total_count = check_status.total_count + len(states)
        passed_count = check_status.passed_count + sum(
            1 for check in states if check.get('state') == 'success'
        )
        if check_status.state in {'failure', 'error'} or legacy_state in {'failure', 'error'}:
            state = 'failure'
        elif check_status.state == 'pending' or legacy_state == 'pending':
            state = 'pending'
        elif total_count:
            state = 'success'
        else:
            state = 'unknown'
        status = CIStatus(
            state=state,
            passed_count=passed_count,
            total_count=total_count,
            pending_approval=check_status.pending_approval,
        )
    else:
        status = check_status
    cache.set(cache_key, status, GUEST_CI_CACHE_SECONDS)
    return status


def enrich_guest_pull_requests(pull_requests):
    """Add diff size and CI state to only the PR cards on the current page."""
    if not pull_requests:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(pull_requests))) as executor:
        futures = {}
        for pr in pull_requests:
            futures[executor.submit(_fetch_public_diff_stats, pr)] = (pr, 'diff')
            futures[executor.submit(_fetch_public_ci_status, pr)] = (pr, 'ci')
        for future in as_completed(futures):
            pr, field = futures[future]
            try:
                result = future.result()
            except Exception:
                logger.exception('Guest PR enrichment failed for %s #%s', pr.repo_full_name, pr.number)
                continue
            if field == 'diff' and result:
                pr.additions = result['additions']
                pr.deletions = result['deletions']
                pr.has_diff_stats = True
                pr.mergeable = result['mergeable']
                pr.auto_merge_enabled = result['auto_merge_enabled']
                pr.auto_merge_enabled_by = result['auto_merge_enabled_by']
            elif field == 'ci' and result:
                pr.ci_status = result
