from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.core.cache import cache
from django.core.paginator import Paginator
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils import timezone
from dataclasses import asdict
import hashlib
import json
import re
import requests

from .models import TrackedRepository, PersonalAccessToken, UserPreferences, AUTO_REFRESH_CATEGORIES
from .github_client import GitHubClient
from .plugin_manager import plugin_manager
from .stats_service import StatsService
from prdash.plugin_api import (
    PR_LIST_PROCESS_HOOK,
    PR_LIST_QUERY_HOOK,
    PullRequestListContext,
    PullRequestQuery,
)

PR_COUNT_CACHE_TTL = 300  # 5 minutes
PR_RESULTS_CACHE_TTL = 3600  # 1 hour, fallback for failed refreshes
PR_RENDER_HASH_CACHE_TTL = 3600  # covers the longest auto-refresh interval with margin


def _compute_pr_render_hash(prs, *, auto_refresh_enabled, auto_refresh_interval, current_username, page_number):
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
    url_pattern = r'github\.com[:/]([^/]+)/([^/\.]+?)(?:\.git)?/?$'
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


def _pr_list_view(request, *, fetch_prs, active_tab, tab_changed,
                  owner=None, repo=None, post_filter=None, post_filter_factory=None,
                  base_fetch_options=None, query_defaults=None, author_required=False):
    """
    Generic PR list view helper.

    Args:
        fetch_prs: Callable(client, repo_tuples_or_owner_repo, fetch_options) -> list of PRs
        active_tab: Value for context['active_tab']
        tab_changed: Value for HX-Trigger tabChanged
        owner/repo: If provided, filters to single repo
        post_filter: Optional callable(prs, username) -> filtered prs
        post_filter_factory: Optional callable(client) -> post_filter function
    """
    repos = TrackedRepository.objects.filter(user=request.user)
    client = GitHubClient(request.user)
    current_username = client.get_username()
    plugin_context = PullRequestListContext(
        request=request,
        client=client,
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
    author = query.parameters.get('author') or None
    if author_required:
        author = request.GET.get('author', '').strip() or None
    fetch_options = dict(base_fetch_options or {})
    fetch_options.update(query.fetch_options)
    if author_required and author:
        fetch_options['author'] = author

    if owner and repo:
        current_repo = get_object_or_404(
            TrackedRepository, user=request.user, owner=owner, name=repo
        )
        prs = fetch_prs(client, owner, repo, fetch_options) if author or not author_required else []
        repo_changed = f'{owner}/{repo}'
    else:
        current_repo = None
        enabled_repos = repos.filter(enabled=True)
        repo_tuples = [(r.owner, r.name) for r in enabled_repos]
        prs = fetch_prs(client, repo_tuples, fetch_options) if author or not author_required else []
        repo_changed = ''

    cache_gen = cache.get(f"pr_results_gen:{request.user.id}", 0)
    cache_vary = dict(query.cache_vary)
    if author_required:
        cache_vary['author'] = author or ''
    query_cache_vary = hashlib.sha256(
        json.dumps(cache_vary, sort_keys=True).encode()
    ).hexdigest()
    results_cache_key = (
        f"pr_results:{request.user.id}:{cache_gen}:{request.path}:"
        f"{query_cache_vary}"
    )
    fetch_had_issues = bool(client.errors or client.warnings)
    stale_data = False
    if fetch_had_issues and not prs:
        # Total failure with no data at all: fall back to the last clean fetch.
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

    # Retrieve all cached counts for sidebar display
    pr_counts = {
        'my_prs': cache.get(f"pr_count:{request.user.id}:my_prs"),
        'review_requests': cache.get(f"pr_count:{request.user.id}:review_requests"),
        'assigned': cache.get(f"pr_count:{request.user.id}:assigned"),
    }

    tab_titles = {
        'review_requests': 'Review Requests',
        'assigned': 'Assigned',
        'merged': 'Merged PRs',
        'author': 'PRs by Author',
        'author_merged': 'PRs by Author',
    }
    page_title = tab_titles.get(active_tab, 'My PRs')
    if current_repo:
        page_title = f'{page_title} - {current_repo.full_name}'
    page_title = f'{page_title} - PR Dashboard'

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
    }

    if request.headers.get('HX-Request') == 'true':
        triggers = {
            'tabChanged': tab_changed,
            'pageTitle': page_title,
            'repoChanged': repo_changed,
            'staleData': stale_data,
        }
        if not stale_data:
            triggers['refreshedAt'] = timezone.now().isoformat()
        triggers.update(client.get_notification_triggers())

        # Only auto-refresh polls (identified by the triggering element's id) are
        # eligible for the render-skip: they always re-request the URL the DOM
        # already shows, unlike tab/filter navigation which targets a new URL.
        is_auto_refresh_poll = request.headers.get('HX-Trigger') == 'auto-refresh-container'
        if is_auto_refresh_poll:
            render_hash = _compute_pr_render_hash(
                prs,
                auto_refresh_enabled=context['auto_refresh_enabled'],
                auto_refresh_interval=context['auto_refresh_interval'],
                current_username=current_username,
                page_number=page_obj.number if page_obj else None,
            )
            hash_cache_key = f"pr_render_hash:{request.user.id}:{request.get_full_path()}"
            if cache.get(hash_cache_key) == render_hash:
                # Nothing changed since the last poll of this URL: skip re-rendering
                # and tell htmx to leave the existing DOM untouched.
                response = HttpResponse(status=204)
                response['HX-Reswap'] = 'none'
                response['HX-Trigger'] = json.dumps(triggers)
                return response

        response = render(request, 'dashboard/partials/_pr_content.html', context)
        if is_auto_refresh_poll:
            cache.set(hash_cache_key, render_hash, PR_RENDER_HASH_CACHE_TTL)
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    response = render(request, 'dashboard/pr_list.html', context)
    add_never_cache_headers(response)
    return response


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

    return _render_repo_list(request)


@login_required
@require_POST
def remove_repo(request, repo_id):
    """Remove a tracked repository."""
    repo = get_object_or_404(TrackedRepository, id=repo_id, user=request.user)
    repo.delete()
    _invalidate_pr_results_cache(request.user)
    return _render_repo_list(request)


@login_required
@require_POST
def toggle_repo(request, repo_id):
    """Toggle a repository's enabled state."""
    repo = get_object_or_404(TrackedRepository, id=repo_id, user=request.user)
    repo.enabled = not repo.enabled
    repo.save()
    _invalidate_pr_results_cache(request.user)
    return _render_repo_list(request)


def _parse_days_param(value: str) -> int:
    """Parse the days parameter, returning -1 for 'all'."""
    if value == 'all':
        return -1
    try:
        days = int(value)
        if days not in (7, 14, 30, 90, 180, 365):
            return 30
        return days
    except (ValueError, TypeError):
        return 30


@login_required
def stats(request):
    """Stats and analytics page."""
    repos = TrackedRepository.objects.filter(user=request.user)

    days = _parse_days_param(request.GET.get('days', '30'))

    context = {
        'days': days,
        'repos': repos,
    }

    return render(request, 'dashboard/stats.html', context)


@login_required
def stats_content(request):
    """HTMX endpoint that returns the actual stats content."""
    repos = TrackedRepository.objects.filter(user=request.user, enabled=True)
    repo_tuples = [(repo.owner, repo.name) for repo in repos]

    days = _parse_days_param(request.GET.get('days', '30'))

    client = GitHubClient(request.user)
    stats_service = StatsService(client)

    # Fetch all stats
    all_stats = stats_service.get_all_stats(repo_tuples, days)

    context = {
        'days': days,
        'quick_stats': all_stats['quick'],
        'velocity_stats': all_stats['velocity'],
        'review_stats': all_stats['reviews'],
        'health_stats': all_stats['health'],
        'repo_stats': all_stats['repos'],
        'collaboration_stats': all_stats['collaboration'],
        'repos': repos,
    }

    return render(request, 'dashboard/partials/_stats_content.html', context)


@login_required
def settings(request):
    """User settings page."""
    pat = PersonalAccessToken.objects.filter(user=request.user).first()
    prefs = _get_user_preferences(request.user)
    context = {
        'pat': pat,
        'prefs': prefs,
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

    prefs, _ = UserPreferences.objects.update_or_create(
        user=request.user,
        defaults=defaults,
    )

    context = {'prefs': prefs, 'success': True}
    return render(request, 'dashboard/partials/_preferences_form.html', context)


@login_required
@require_POST
def save_plugins(request):
    """Save the explicit set of plugins enabled for the current user."""
    plugins_changed = plugin_manager.configure_user(
        request.user,
        request.POST.getlist('enabled_plugins'),
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
