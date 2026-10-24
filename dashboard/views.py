from django.shortcuts import render, redirect, get_object_or_404
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods, require_POST
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import connections
from django.urls import reverse
from django.utils.cache import add_never_cache_headers, patch_vary_headers
from django.utils import timezone
from dataclasses import asdict
import hashlib
import json
import logging
from mimetypes import guess_type
import re
import requests
import threading

from .models import TrackedRepository, PersonalAccessToken, UserPreferences, AUTO_REFRESH_CATEGORIES
from .github_client import GitHubClient
from .plugin_manager import plugin_manager
from prdash.plugin_api import (
    PR_LIST_PROCESS_HOOK,
    PR_LIST_QUERY_HOOK,
    PR_SOURCE_OPEN_ALL,
    PullRequestListContext,
    PullRequestQuery,
)

PR_COUNT_CACHE_TTL = 300  # 5 minutes
PR_RESULTS_CACHE_TTL = 3600  # 1 hour, fallback for failed refreshes
PR_SNAPSHOT_CACHE_TTL = 86400  # 1 day, the list a fetch-on-demand page renders
PR_FETCH_JOB_RETENTION = 3600  # Seconds a finished fetch job stays queryable

logger = logging.getLogger(__name__)

# Keyed by _pr_fetch_key().
_pr_fetch_jobs = {}
_pr_fetch_jobs_lock = threading.Lock()


def _compute_pr_render_hash(
    prs, *, auto_refresh_enabled, auto_refresh_interval, current_username, page_number, stale_data, render_path,
    pr_fetch=None,
):
    """Hash the data that determines the rendered _pr_content.html output for a poll.

    pr_counts/errors/warnings are excluded: _pr_content.html doesn't render
    them (counts are sidebar-only, errors/warnings only surface as toasts on
    a full page load), so they can't affect the swapped partial's HTML.
    """
    payload = json.dumps(
        {
            'prs': [asdict(pr) for pr in prs],
            'auto_refresh_enabled': auto_refresh_enabled,
            'auto_refresh_interval': auto_refresh_interval,
            'current_username': current_username,
            'page_number': page_number,
            'stale_data': stale_data,
            'render_path': render_path,
            'pr_fetch': pr_fetch and {
                'snapshot': pr_fetch['snapshot'] and {
                    'fetched_at': pr_fetch['snapshot']['fetched_at'],
                    'partial': pr_fetch['snapshot']['partial'],
                },
                'job_state': (pr_fetch['job'] or {}).get('state'),
            },
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _invalidate_pr_results_cache(user):
    """Bump the PR results cache generation so stale fallback data is no longer served."""
    key = f"pr_results_gen:{user.id}"
    cache.add(key, 0)
    cache.incr(key)


def _invalidate_repo_edit_caches(user):
    """Invalidate PR results and tab counts after a tracked repository changes."""
    _invalidate_pr_results_cache(user)
    cache.delete_many([
        f"pr_count:{user.id}:my_prs",
        f"pr_count:{user.id}:review_requests",
        f"pr_count:{user.id}:assigned",
    ])


def _get_user_preferences(user):
    """Get or create user preferences with defaults."""
    prefs, _ = UserPreferences.objects.get_or_create(user=user)
    return prefs


def _exclude_own_prs(prs, username):
    """Filter out PRs authored by the given user."""
    return [pr for pr in prs if pr.author != username]


def _parse_repo_input(repo_input):
    """
    Parse repository input in various formats and return (owner, name) tuple.
    Supports:
    - owner/repo
    - https://github.com/owner/repo
    - https://github.com/owner/repo.git
    - git@github.com:owner/repo.git
    """
    repo_input = repo_input.strip()

    # GitHub URL patterns
    url_pattern = r'github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$'
    match = re.search(url_pattern, repo_input)
    if match:
        return match.group(1).strip(), match.group(2).strip()

    # Simple owner/repo format
    if '/' in repo_input:
        parts = repo_input.split('/', 1)
        owner = parts[0].strip()
        name = parts[1].strip().removesuffix('.git')
        return owner, name

    return None, None


def home(request):
    """Landing page - redirect to PRs if logged in."""
    if request.user.is_authenticated:
        return redirect('dashboard:pr_list')
    return render(request, 'dashboard/home.html')


def _run_pr_list_query(request, current_username, active_tab, owner=None, repo=None, query_defaults=None):
    plugin_context = PullRequestListContext(
        request=request,
        active_tab=active_tab,
        current_username=current_username,
        current_repo=(owner, repo) if owner and repo else None,
        query_defaults=query_defaults or {},
    )
    query = plugin_manager.run_hook(
        PR_LIST_QUERY_HOOK,
        PullRequestQuery(),
        plugin_context,
        request.user,
        request,
    )
    plugin_context.query = query
    return plugin_context, query


def _query_cache_vary(query, author=None):
    cache_vary = dict(query.cache_vary)
    if author is not None:
        cache_vary['author'] = author
    return hashlib.sha256(json.dumps(cache_vary, sort_keys=True).encode()).hexdigest()


def _cached_pr_counts(user):
    return {
        'my_prs': cache.get(f"pr_count:{user.id}:my_prs"),
        'review_requests': cache.get(f"pr_count:{user.id}:review_requests"),
        'assigned': cache.get(f"pr_count:{user.id}:assigned"),
    }


def _pr_list_page_title(active_tab, plugin_page, current_repo):
    tab_titles = {
        'review_requests': 'Review Requests',
        'assigned': 'Assigned',
        'merged': 'Merged PRs',
        'author': 'PRs by Author',
        'author_merged': 'PRs by Author',
    }
    page_title = plugin_page['title'] if plugin_page else tab_titles.get(active_tab, 'My PRs')
    if current_repo:
        page_title = f'{page_title} - {current_repo.full_name}'
    return f'{page_title} - PR Dashboard'


def _render_pr_list_page(request, context):
    context['plugin_pages'] = plugin_manager.pr_list_pages(request.user, request)
    response = render(request, 'dashboard/pr_list.html', context)
    add_never_cache_headers(response)
    patch_vary_headers(response, ['HX-Request'])
    return response


def _pr_list_shell(request, repos, active_tab, owner, repo, plugin_page, query, author):
    """Serve everything except the PR list right away. The list loads via hx-trigger="load"."""
    current_repo = None
    if owner and repo:
        current_repo = get_object_or_404(
            TrackedRepository, user=request.user, owner=owner, name=repo
        )
    user_prefs = _get_user_preferences(request.user)
    context = {
        'repos': repos,
        'current_repo': current_repo,
        'active_tab': active_tab,
        'author': author,
        'filters': query.parameters,
        'has_active_filters': query.affects_count,
        'auto_refresh_enabled': user_prefs.is_auto_refresh_enabled_for_tab(active_tab),
        'auto_refresh_interval': user_prefs.get_auto_refresh_interval_seconds_for_tab(active_tab),
        'auto_refresh_interval_mins': user_prefs.get_auto_refresh_interval_for_tab(active_tab),
        'pr_counts': _cached_pr_counts(request.user),
        'page_title': _pr_list_page_title(active_tab, plugin_page, current_repo),
        'plugin_page': plugin_page,
        'defer_content_url': request.get_full_path(),
    }
    return _render_pr_list_page(request, context)


def _pr_list_view(request, *, fetch_prs, active_tab, tab_changed,
                  owner=None, repo=None, post_filter=None, post_filter_factory=None,
                  base_fetch_options=None, query_defaults=None, author_required=False,
                  plugin_page=None):
    """
    Generic PR list view helper.

    Args:
        fetch_prs: Callable(client, repo_tuples_or_owner_repo, fetch_options) -> list of PRs
        active_tab: Value for context['active_tab']
        tab_changed: Value for HX-Trigger tabChanged
        owner/repo: If provided, filters to single repo
        post_filter: Optional callable(prs, username) -> filtered prs
        post_filter_factory: Optional callable(client) -> post_filter function
        plugin_page: The plugin PR list page being served, from plugin_manager.pr_list_page
    """
    repos = TrackedRepository.objects.filter(user=request.user)
    fetch_on_demand = bool(plugin_page and plugin_page['fetch_on_demand'])

    client = GitHubClient(request.user)
    current_username = client.get_username()
    plugin_context, query = _run_pr_list_query(
        request, current_username, active_tab, owner, repo, query_defaults,
    )
    author = query.parameters.get('author') or None
    if author_required:
        author = request.GET.get('author', '').strip() or None
    if request.headers.get('HX-Request') != 'true' and not fetch_on_demand:
        return _pr_list_shell(request, repos, active_tab, owner, repo, plugin_page, query, author)

    fetch_options = dict(base_fetch_options or {})
    fetch_options.update(query.fetch_options)
    if author_required and author:
        fetch_options['author'] = author

    cache_gen = cache.get(f"pr_results_gen:{request.user.id}", 0)
    query_cache_vary = _query_cache_vary(query, (author or '') if author_required else None)
    pr_fetch = None

    if owner and repo:
        current_repo = get_object_or_404(
            TrackedRepository, user=request.user, owner=owner, name=repo
        )
        prs = fetch_prs(client, owner, repo, fetch_options) if author or not author_required else []
        repo_changed = f'{owner}/{repo}'
    elif fetch_on_demand:
        current_repo = None
        pr_fetch = _pr_fetch_context(request.user, plugin_page, query_cache_vary)
        prs = list(pr_fetch['snapshot']['prs']) if pr_fetch['snapshot'] else []
        repo_changed = ''
    else:
        current_repo = None
        enabled_repos = repos.filter(enabled=True)
        repo_tuples = [(r.owner, r.name) for r in enabled_repos]
        prs = fetch_prs(client, repo_tuples, fetch_options) if author or not author_required else []
        repo_changed = ''

    results_cache_key = (
        f"pr_results:{request.user.id}:{cache_gen}:{request.path}:"
        f"{query_cache_vary}"
    )
    fetch_had_issues = bool(client.errors or client.warnings)
    stale_data = False
    if fetch_on_demand:
        # Nothing was fetched, so the snapshot is only stale if its own fetch was partial.
        stale_data = bool(pr_fetch['snapshot'] and pr_fetch['snapshot']['partial'])
    elif fetch_had_issues:
        # A partial result is not a complete snapshot, so prefer the last clean fetch.
        cached_prs = cache.get(results_cache_key)
        if cached_prs is not None:
            prs = cached_prs
            stale_data = True
    elif not fetch_had_issues:
        # Only a fully clean fetch is trustworthy enough to become the new fallback.
        cache.set(results_cache_key, prs, PR_RESULTS_CACHE_TTL)

    if post_filter_factory:
        post_filter = post_filter_factory(client)
    if post_filter and current_username:
        prs = post_filter(prs, current_username)

    prs = plugin_manager.run_hook(
        PR_LIST_PROCESS_HOOK,
        prs,
        plugin_context,
        request.user,
        request,
    )

    user_prefs = _get_user_preferences(request.user)

    # Cache the count for this tab (only when no filters are active)
    # Map active_tab to cache key
    tab_to_cache_key = {
        'open': 'my_prs',
        'review_requests': 'review_requests',
        'assigned': 'assigned',
    }
    cache_key_suffix = tab_to_cache_key.get(active_tab)
    if cache_key_suffix and not current_repo and not query.affects_count:
        # Only cache when viewing all repos with no filters
        count_cache_key = f"pr_count:{request.user.id}:{cache_key_suffix}"
        cache.set(count_cache_key, len(prs), PR_COUNT_CACHE_TTL)

    page_obj = None
    page_size = user_prefs.pr_list_page_size
    if page_size:
        paginator = Paginator(prs, page_size)
        try:
            page_number = int(request.GET.get('page', 1))
        except (ValueError, TypeError):
            page_number = 1
        page_number = max(1, min(page_number, paginator.num_pages))
        page_obj = paginator.page(page_number)
        prs = page_obj.object_list

    pr_counts = _cached_pr_counts(request.user)
    page_title = _pr_list_page_title(active_tab, plugin_page, current_repo)

    render_hash = _compute_pr_render_hash(
        prs,
        auto_refresh_enabled=user_prefs.is_auto_refresh_enabled_for_tab(active_tab),
        auto_refresh_interval=user_prefs.get_auto_refresh_interval_seconds_for_tab(active_tab),
        current_username=current_username,
        page_number=page_obj.number if page_obj else None,
        stale_data=stale_data,
        render_path=request.get_full_path(),
        pr_fetch=pr_fetch,
    )

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': current_repo,
        'active_tab': active_tab,
        'author': author,
        'current_username': current_username,
        'filters': query.parameters,
        'has_active_filters': query.affects_count,
        'errors': client.errors,
        'warnings': client.warnings,
        'auto_refresh_enabled': user_prefs.is_auto_refresh_enabled_for_tab(active_tab),
        'auto_refresh_interval': user_prefs.get_auto_refresh_interval_seconds_for_tab(active_tab),
        'auto_refresh_interval_mins': user_prefs.get_auto_refresh_interval_for_tab(active_tab),
        'pr_counts': pr_counts,
        'page_obj': page_obj,
        'page_title': page_title,
        'stale_data': stale_data,
        'pr_render_hash': render_hash,
        'plugin_page': plugin_page,
        'pr_fetch': pr_fetch,
    }

    if request.headers.get('HX-Request') == 'true':
        triggers = {
            'tabChanged': tab_changed,
            'pageTitle': page_title,
            'repoChanged': repo_changed,
            'staleData': stale_data,
            'prCounts': pr_counts,
        }
        if not stale_data and not fetch_on_demand:
            triggers['refreshedAt'] = timezone.now().isoformat()
        triggers.update(client.get_notification_triggers())

        # Only auto-refresh polls (identified by the triggering element's id) are
        # eligible for the render-skip: they always re-request the URL the DOM
        # already shows, unlike tab/filter navigation which targets a new URL.
        is_auto_refresh_poll = request.headers.get('HX-Trigger') == 'auto-refresh-container'
        if is_auto_refresh_poll and request.headers.get('X-PR-Render-Hash') == render_hash:
            # Nothing changed since this browser rendered the partial, so leave its DOM untouched.
            response = HttpResponse(status=204)
            response['HX-Reswap'] = 'none'
            response['HX-Trigger'] = json.dumps(triggers)
            return response

        response = render(request, 'dashboard/partials/_pr_content.html', context)
        patch_vary_headers(response, ['HX-Request'])
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return _render_pr_list_page(request, context)


@login_required
def pr_list(request):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, repos, options: c.get_all_user_prs(repos, **options),
        active_tab='open',
        tab_changed='my_prs',
    )


@login_required
def merged_pr_list(request):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, repos, options: c.get_all_merged_prs(repos, **options),
        active_tab='merged',
        tab_changed='merged',
    )


@login_required
def author_pr_list(request):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, repos, options: c.get_all_user_prs(repos, **options),
        active_tab='author',
        tab_changed='author_prs',
        author_required=True,
    )


@login_required
def author_merged_pr_list(request):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, repos, options: c.get_all_merged_prs(repos, **options),
        active_tab='author_merged',
        tab_changed='author_merged',
        author_required=True,
    )


@login_required
def repo_pr_list(request, owner, repo):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, o, r, options: c.get_user_prs_for_repo(o, r, **options),
        active_tab='open',
        tab_changed='my_prs',
        owner=owner,
        repo=repo,
    )


@login_required
def repo_merged_pr_list(request, owner, repo):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, o, r, options: c.get_merged_prs_for_repo(o, r, **options),
        active_tab='merged',
        tab_changed='merged',
        owner=owner,
        repo=repo,
    )


@login_required
def review_requests_list(request):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, repos, options: c.get_all_review_requests(
            repos, **options
        ),
        active_tab='review_requests',
        tab_changed='review_requests',
        post_filter=_exclude_own_prs,
        base_fetch_options={'include_all': True},
    )


@login_required
def review_approved_list(request):
    return redirect(f"{reverse('dashboard:review_requests_list')}?my_review=approved")


@login_required
def review_reviewed_list(request):
    return redirect(f"{reverse('dashboard:review_requests_list')}?my_review=reviewed")


@login_required
def assigned_list(request):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, repos, options: c.get_all_assigned_prs(repos, **options),
        active_tab='assigned',
        tab_changed='assigned',
    )


@login_required
def repo_review_requests_list(request, owner, repo):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, o, r, options: c.get_review_requests_for_repo(
            o, r, **options
        ),
        active_tab='review_requests',
        tab_changed='review_requests',
        owner=owner,
        repo=repo,
        post_filter=_exclude_own_prs,
        base_fetch_options={'include_all': True},
    )


@login_required
def repo_review_approved_list(request, owner, repo):
    url = reverse(
        'dashboard:repo_review_requests_list',
        kwargs={'owner': owner, 'repo': repo},
    )
    return redirect(f'{url}?my_review=approved')


@login_required
def repo_review_reviewed_list(request, owner, repo):
    url = reverse(
        'dashboard:repo_review_requests_list',
        kwargs={'owner': owner, 'repo': repo},
    )
    return redirect(f'{url}?my_review=reviewed')


@login_required
def repo_assigned_list(request, owner, repo):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, o, r, options: c.get_assigned_prs_for_repo(o, r, **options),
        active_tab='assigned',
        tab_changed='assigned',
        owner=owner,
        repo=repo,
    )


_PLUGIN_PAGE_FETCHERS = {
    PR_SOURCE_OPEN_ALL: lambda c, repos, options: c.get_all_open_prs(
        repos, exclude_author=c.get_username(), **options
    ),
}


@login_required
def plugin_pr_list(request, plugin_id, name):
    page = plugin_manager.pr_list_page(request.user, plugin_id, name, request)
    if page is None or page['source'] not in _PLUGIN_PAGE_FETCHERS:
        raise Http404
    return _pr_list_view(
        request,
        fetch_prs=_PLUGIN_PAGE_FETCHERS[page['source']],
        active_tab=page['tab'],
        tab_changed=page['tab'],
        post_filter=_exclude_own_prs,
        plugin_page=page,
    )


def _pr_fetch_key(user, page, query_cache_vary):
    cache_gen = cache.get(f"pr_results_gen:{user.id}", 0)
    return f"{user.id}:{cache_gen}:{page['tab']}:{query_cache_vary}"


def _pr_fetch_job(key):
    with _pr_fetch_jobs_lock:
        job = _pr_fetch_jobs.get(key)
        if job is None:
            return None
        if job['state'] == 'running' and not job['thread'].is_alive():
            job.update(state='failed', errors=['The fetch stopped unexpectedly'], finished_at=timezone.now())
        state = {name: value for name, value in job.items() if name != 'thread'}
    state['elapsed_seconds'] = round(((state['finished_at'] or timezone.now()) - state['started_at']).total_seconds())
    return state


def _pr_fetch_context(user, page, query_cache_vary):
    key = _pr_fetch_key(user, page, query_cache_vary)
    return {
        'url': reverse('dashboard:plugin_pr_list_fetch', args=[page['plugin_id'], page['name']]),
        'snapshot': cache.get(f'pr_snapshot:{key}'),
        'job': _pr_fetch_job(key),
    }


def _start_pr_fetch(user, key, fetch_prs, repo_tuples, fetch_options):
    """Start a background fetch unless one for the same key is running."""
    now = timezone.now()
    with _pr_fetch_jobs_lock:
        for job_key, job in list(_pr_fetch_jobs.items()):
            if job['finished_at'] and (now - job['finished_at']).total_seconds() > PR_FETCH_JOB_RETENTION:
                del _pr_fetch_jobs[job_key]
        job = _pr_fetch_jobs.get(key)
        if job and job['state'] == 'running' and job['thread'].is_alive():
            return
        job = {
            'state': 'running', 'phase': 'Starting', 'done': 0, 'total': 0, 'count': 0,
            'errors': [], 'warnings': [], 'started_at': now, 'finished_at': None,
        }
        job['thread'] = threading.Thread(
            target=_run_pr_fetch, args=(user, key, job, fetch_prs, repo_tuples, fetch_options), daemon=True,
        )
        _pr_fetch_jobs[key] = job
        job['thread'].start()


def _run_pr_fetch(user, key, job, fetch_prs, repo_tuples, fetch_options):
    def progress(phase, done, total):
        with _pr_fetch_jobs_lock:
            job.update(phase=phase, done=done, total=total)

    try:
        client = GitHubClient(user)
        prs = fetch_prs(client, repo_tuples, {**fetch_options, 'progress': progress})
        partial = bool(client.errors or client.warnings)
        snapshot_key = f'pr_snapshot:{key}'
        previous = cache.get(snapshot_key)
        # Like pr_results, a partial fetch does not replace a complete earlier one.
        if not partial or previous is None or previous['partial']:
            cache.set(
                snapshot_key, {'prs': prs, 'fetched_at': timezone.now(), 'partial': partial}, PR_SNAPSHOT_CACHE_TTL,
            )
        result = {'state': 'done', 'count': len(prs), 'errors': client.errors, 'warnings': client.warnings}
    except Exception:
        logger.exception('Fetching pull requests failed for %s', key)
        result = {'state': 'failed', 'errors': ['Fetching pull requests failed']}
    finally:
        connections.close_all()
    with _pr_fetch_jobs_lock:
        job.update(result, finished_at=timezone.now())


@login_required
@require_http_methods(['GET', 'POST'])
def plugin_pr_list_fetch(request, plugin_id, name):
    """The query string must match the page so plugin query hooks pick the same fetch options."""
    page = plugin_manager.pr_list_page(request.user, plugin_id, name, request)
    if page is None or not page['fetch_on_demand'] or page['source'] not in _PLUGIN_PAGE_FETCHERS:
        raise Http404
    _, query = _run_pr_list_query(request, GitHubClient(request.user).get_username(), page['tab'])
    key = _pr_fetch_key(request.user, page, _query_cache_vary(query))
    if request.method == 'POST':
        repo_tuples = [(r.owner, r.name) for r in TrackedRepository.objects.filter(user=request.user, enabled=True)]
        _start_pr_fetch(request.user, key, _PLUGIN_PAGE_FETCHERS[page['source']], repo_tuples, query.fetch_options)
    response = JsonResponse(_pr_fetch_job(key) or {'state': 'idle'})
    add_never_cache_headers(response)
    return response


def _render_repo_list(request, trigger='repoToggled', errors=None):
    """Render the repo list partial with appropriate HX-Trigger."""
    repos = TrackedRepository.objects.filter(user=request.user)
    response = render(request, 'dashboard/partials/_repo_list.html', {'repos': repos})
    if errors:
        response['HX-Trigger'] = json.dumps({'showErrors': errors})
    else:
        response['HX-Trigger'] = trigger
    return response


@login_required
@require_POST
def add_repo(request):
    """Add a new repository to track."""
    repo_input = request.POST.get('repo', '').strip()
    owner, name = _parse_repo_input(repo_input)

    if not owner or not name:
        return _render_repo_list(request, errors=['Invalid format. Use owner/repo'])

    client = GitHubClient(request.user)
    valid, message = client.validate_repo(owner, name)
    if not valid:
        return _render_repo_list(request, errors=[message])

    repo, created = TrackedRepository.objects.get_or_create(
        user=request.user,
        owner=owner,
        name=name
    )
    if not created:
        return _render_repo_list(request, errors=['Repository already tracked'])

    _invalidate_repo_edit_caches(request.user)
    return _render_repo_list(request)


@login_required
@require_POST
def remove_repo(request, repo_id):
    """Remove a tracked repository."""
    repo = get_object_or_404(TrackedRepository, id=repo_id, user=request.user)
    repo.delete()
    _invalidate_repo_edit_caches(request.user)
    return _render_repo_list(request)


@login_required
@require_POST
def toggle_repo(request, repo_id):
    """Toggle a repository's enabled state."""
    repo = get_object_or_404(TrackedRepository, id=repo_id, user=request.user)
    repo.enabled = not repo.enabled
    repo.save()
    _invalidate_repo_edit_caches(request.user)
    return _render_repo_list(request)


@login_required
def settings(request):
    """User settings page."""
    pat = PersonalAccessToken.objects.filter(user=request.user).first()
    prefs = _get_user_preferences(request.user)
    context = {
        'pat': pat,
        'prefs': prefs,
        'auto_refresh_rows': prefs.get_auto_refresh_rows(plugin_manager.pr_list_pages(request.user, request)),
        'plugins': plugin_manager.plugin_statuses(request.user, request),
        'plugin_discovery_errors': plugin_manager.discovery_errors,
    }
    return render(request, 'dashboard/settings.html', context)


@login_required
@require_POST
def save_pat(request):
    """Save or update Personal Access Token."""
    token = request.POST.get('token', '').strip()

    if not token:
        PersonalAccessToken.objects.filter(user=request.user).delete()
        pat = None
    else:
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
        error = None
        try:
            resp = requests.get('https://api.github.com/user', headers=headers, timeout=10)
            if resp.status_code == 401:
                error = 'Invalid token: Bad credentials'
            elif resp.status_code == 403:
                fallback = requests.get('https://api.github.com/rate_limit', headers=headers, timeout=10)
                if fallback.status_code != 200:
                    error = f'Invalid token: HTTP {fallback.status_code}'
        except requests.RequestException as e:
            error = f'Failed to validate token: {e}'

        if error:
            pat = PersonalAccessToken.objects.filter(user=request.user).first()
            return render(request, 'dashboard/partials/_pat_form.html', {'pat': pat, 'error': error})

        pat, _ = PersonalAccessToken.objects.update_or_create(
            user=request.user,
            defaults={'token': token}
        )

    _invalidate_pr_results_cache(request.user)
    context = {'pat': pat, 'success': True}
    return render(request, 'dashboard/partials/_pat_form.html', context)


@login_required
@require_POST
def delete_pat(request):
    """Delete Personal Access Token."""
    PersonalAccessToken.objects.filter(user=request.user).delete()
    _invalidate_pr_results_cache(request.user)
    context = {'pat': None, 'deleted': True}
    return render(request, 'dashboard/partials/_pat_form.html', context)


@login_required
@require_POST
def save_preferences(request):
    """Save user preferences."""
    valid_intervals = [choice[0] for choice in UserPreferences._meta.get_field('auto_refresh_interval_open').choices]
    valid_page_sizes = [choice[0] for choice in UserPreferences._meta.get_field('pr_list_page_size').choices]

    def parse_interval(field_name):
        try:
            interval = int(request.POST.get(field_name, 5))
        except (ValueError, TypeError):
            return 5
        return interval if interval in valid_intervals else 5

    def parse_page_size():
        try:
            page_size = int(request.POST.get('pr_list_page_size', 25))
        except (ValueError, TypeError):
            return 25
        return page_size if page_size in valid_page_sizes else 25

    defaults = {
        'pr_list_page_size': parse_page_size(),
    }
    for category, _ in AUTO_REFRESH_CATEGORIES:
        defaults[f'auto_refresh_{category}'] = request.POST.get(f'auto_refresh_{category}') == 'on'
        defaults[f'auto_refresh_interval_{category}'] = parse_interval(f'auto_refresh_interval_{category}')

    # Pages of currently disabled plugins keep their saved settings.
    plugin_pages = plugin_manager.pr_list_pages(request.user, request)
    plugin_auto_refresh = dict(_get_user_preferences(request.user).plugin_auto_refresh)
    for page in plugin_pages:
        tab = page['tab']
        plugin_auto_refresh[tab] = {
            'enabled': request.POST.get(f'auto_refresh_{tab}') == 'on',
            'interval': parse_interval(f'auto_refresh_interval_{tab}'),
        }
    defaults['plugin_auto_refresh'] = plugin_auto_refresh

    prefs, _ = UserPreferences.objects.update_or_create(
        user=request.user,
        defaults=defaults,
    )

    context = {'prefs': prefs, 'auto_refresh_rows': prefs.get_auto_refresh_rows(plugin_pages), 'success': True}
    return render(request, 'dashboard/partials/_preferences_form.html', context)


@login_required
@require_POST
def save_plugins(request):
    """Save the explicit set of plugins enabled for the current user."""
    plugins_changed = plugin_manager.configure_user(
        request.user,
        request.POST.getlist('enabled_plugins'),
        {
            plugin_id: request.POST.getlist(f'permissions_{plugin_id}')
            for plugin_id in request.POST.getlist('enabled_plugins')
        },
    )
    context = {
        'plugins': plugin_manager.plugin_statuses(request.user),
        'plugin_discovery_errors': plugin_manager.discovery_errors,
        'success': True,
        'plugins_changed': plugins_changed,
    }
    return render(request, 'dashboard/partials/_plugin_settings.html', context)


@login_required
def plugin_route(request, plugin_id, route):
    """Dispatch a request to a route registered by an enabled plugin."""
    return plugin_manager.dispatch(request, plugin_id, route)


def plugin_asset(request, plugin_id, asset_name):
    """Serve a file from an installed plugin package static directory."""
    asset_path = plugin_manager.asset_path(plugin_id, asset_name)
    try:
        content = asset_path.read_bytes()
    except OSError as error:
        raise Http404 from error
    content_type = guess_type(asset_path.name)[0] or 'application/octet-stream'
    response = HttpResponse(content, content_type=content_type)
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'no-cache'
    return response


@login_required
def plugin_route_stream(request, plugin_id, route):
    """Dispatch a request to a plugin route, relaying its progress chunks over SSE."""
    def event_stream():
        try:
            for event in plugin_manager.dispatch_stream(request, plugin_id, route):
                yield f'data: {json.dumps(event)}\n\n'
        except Http404:
            yield f'data: {json.dumps({"final": True, "error": "not_found"})}\n\n'

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response
