from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.core.cache import cache
import json
import re
import requests

from .models import TrackedRepository, PersonalAccessToken, UserPreferences
from .github_client import GitHubClient
from .stats_service import StatsService

PR_COUNT_CACHE_TTL = 300  # 5 minutes
PR_RESULTS_CACHE_TTL = 3600  # 1 hour, fallback for failed refreshes


def _invalidate_pr_results_cache(user):
    """Bump the PR results cache generation so stale fallback data is no longer served."""
    key = f"pr_results_gen:{user.id}"
    cache.add(key, 0)
    cache.incr(key)


def _get_user_preferences(user):
    """Get or create user preferences with defaults."""
    prefs, _ = UserPreferences.objects.get_or_create(user=user)
    return prefs


def _get_filter_params(request):
    """Extract filter and sort parameters from request."""
    return {
        'ci': request.GET.get('ci', ''),
        'review': request.GET.get('review', ''),
        'my_review': request.GET.get('my_review', ''),
        'draft': request.GET.get('draft', ''),
        'conflicts': request.GET.get('conflicts', ''),
        'sort': request.GET.get('sort', 'updated_desc'),
    }


def _exclude_own_prs(prs, username):
    """Filter out PRs authored by the given user."""
    return [pr for pr in prs if pr.author != username]


def _apply_filters_and_sort(prs, filters):
    """Apply filters and sorting to PR list."""
    ci = filters.get('ci')
    review = filters.get('review')
    draft = filters.get('draft')
    conflicts = filters.get('conflicts')
    sort = filters.get('sort', 'updated_desc')

    if ci:
        prs = [p for p in prs if p.ci_status.state == ci]
    if review:
        prs = [p for p in prs if p.review_status.state == review]
    if draft == 'ready':
        prs = [p for p in prs if not p.draft]
    elif draft == 'draft':
        prs = [p for p in prs if p.draft]
    if conflicts == 'has':
        prs = [p for p in prs if p.mergeable == 'CONFLICTING']
    elif conflicts == 'none':
        prs = [p for p in prs if p.mergeable == 'MERGEABLE']

    sort_keys = {
        'updated': lambda p: (p.updated_at, p.number),
        'created': lambda p: (p.created_at, p.number),
    }
    sort_field = sort.replace('_desc', '').replace('_asc', '')
    sort_key = sort_keys.get(sort_field, sort_keys['updated'])
    reverse = sort.endswith('_desc') or not sort.endswith('_asc')
    prs = sorted(prs, key=sort_key, reverse=reverse)

    return prs


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


def _pr_list_view(request, *, fetch_prs, active_tab, tab_changed, review_tab='pending',
                  owner=None, repo=None, post_filter=None, post_filter_factory=None):
    """
    Generic PR list view helper.

    Args:
        fetch_prs: Callable(client, repo_tuples_or_owner_repo, author) -> list of PRs
        active_tab: Value for context['active_tab']
        tab_changed: Value for HX-Trigger tabChanged
        review_tab: Value for context['review_tab'] and reviewTabChanged trigger
        owner/repo: If provided, filters to single repo
        post_filter: Optional callable(prs, username) -> filtered prs
        post_filter_factory: Optional callable(client) -> post_filter function
    """
    repos = TrackedRepository.objects.filter(user=request.user)
    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    filters = _get_filter_params(request)

    if owner and repo:
        current_repo = get_object_or_404(
            TrackedRepository, user=request.user, owner=owner, name=repo
        )
        prs = fetch_prs(client, owner, repo, author)
        repo_changed = f'{owner}/{repo}'
    else:
        current_repo = None
        enabled_repos = repos.filter(enabled=True)
        repo_tuples = [(r.owner, r.name) for r in enabled_repos]
        prs = fetch_prs(client, repo_tuples, author)
        repo_changed = ''

    cache_gen = cache.get(f"pr_results_gen:{request.user.id}", 0)
    results_cache_key = (
        f"pr_results:{request.user.id}:{cache_gen}:{request.path}:{author or ''}:"
        f"{request.GET.get('my_review', '')}"
    )
    fetch_had_issues = bool(client.errors or client.warnings)
    if fetch_had_issues and not prs:
        # Total failure with no data at all: fall back to the last clean fetch.
        cached_prs = cache.get(results_cache_key)
        if cached_prs is not None:
            prs = cached_prs
    elif not fetch_had_issues:
        # Only a fully clean fetch is trustworthy enough to become the new fallback.
        cache.set(results_cache_key, prs, PR_RESULTS_CACHE_TTL)

    if post_filter_factory:
        post_filter = post_filter_factory(client)
    if post_filter and current_username:
        prs = post_filter(prs, current_username)

    prs = _apply_filters_and_sort(prs, filters)

    user_prefs = _get_user_preferences(request.user)

    # Cache the count for this tab (only when no filters are active)
    # Map active_tab to cache key
    tab_to_cache_key = {
        'open': 'my_prs',
        'review_requests': 'review_requests',
        'assigned': 'assigned',
    }
    cache_key_suffix = tab_to_cache_key.get(active_tab)
    has_filters = any(filters.get(k) for k in ('ci', 'review', 'my_review', 'draft', 'conflicts'))
    if cache_key_suffix and not author and not current_repo and not has_filters:
        # Only cache when viewing all repos with no filters
        count_cache_key = f"pr_count:{request.user.id}:{cache_key_suffix}"
        cache.set(count_cache_key, len(prs), PR_COUNT_CACHE_TTL)

    # Retrieve all cached counts for sidebar display
    pr_counts = {
        'my_prs': cache.get(f"pr_count:{request.user.id}:my_prs"),
        'review_requests': cache.get(f"pr_count:{request.user.id}:review_requests"),
        'assigned': cache.get(f"pr_count:{request.user.id}:assigned"),
    }

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': current_repo,
        'active_tab': active_tab,
        'author': author,
        'current_username': current_username,
        'filters': filters,
        'errors': client.errors,
        'warnings': client.warnings,
        'auto_refresh_enabled': user_prefs.is_auto_refresh_enabled_for_tab(active_tab),
        'auto_refresh_interval': user_prefs.auto_refresh_interval_seconds,
        'auto_refresh_interval_mins': user_prefs.auto_refresh_interval,
        'pr_counts': pr_counts,
    }
    if review_tab != 'pending':
        context['review_tab'] = review_tab

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': tab_changed, 'repoChanged': repo_changed, 'reviewTabChanged': review_tab}
        triggers.update(client.get_notification_triggers())
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def pr_list(request):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, repos, author: c.get_all_user_prs(repos, author=author),
        active_tab='open',
        tab_changed='my_prs',
    )


@login_required
def merged_pr_list(request):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, repos, author: c.get_all_merged_prs(repos, author=author),
        active_tab='merged',
        tab_changed='merged',
    )


@login_required
def repo_pr_list(request, owner, repo):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, o, r, author: c.get_user_prs_for_repo(o, r, author=author),
        active_tab='open',
        tab_changed='my_prs',
        owner=owner,
        repo=repo,
    )


@login_required
def repo_merged_pr_list(request, owner, repo):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, o, r, author: c.get_merged_prs_for_repo(o, r, author=author),
        active_tab='merged',
        tab_changed='merged',
        owner=owner,
        repo=repo,
    )


def _get_review_fetch_params(my_review):
    """Get fetch parameters based on my_review filter value."""
    if my_review == 'approved':
        return {'approved_by_me': True, 'reviewed_by_me': False}
    elif my_review == 'reviewed':
        return {'approved_by_me': False, 'reviewed_by_me': True}
    elif my_review == 'pending':
        return {'approved_by_me': False, 'reviewed_by_me': False}
    else:
        # Default: show all review requests (pending + reviewed + approved)
        return {'include_all': True}


@login_required
def review_requests_list(request):
    my_review = request.GET.get('my_review', '')
    fetch_params = _get_review_fetch_params(my_review)

    return _pr_list_view(
        request,
        fetch_prs=lambda c, repos, author: c.get_all_review_requests(
            repos, author=author, **fetch_params
        ),
        active_tab='review_requests',
        tab_changed='review_requests',
        post_filter=_exclude_own_prs,
    )


@login_required
def review_approved_list(request):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, repos, author: c.get_all_review_requests(repos, approved_by_me=True, author=author),
        active_tab='review_requests',
        tab_changed='review_approved',
        review_tab='approved',
        post_filter=_exclude_own_prs,
    )


@login_required
def review_reviewed_list(request):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, repos, author: c.get_all_review_requests(repos, reviewed_by_me=True, author=author),
        active_tab='review_requests',
        tab_changed='review_reviewed',
        review_tab='reviewed',
        post_filter=_exclude_own_prs,
    )


@login_required
def assigned_list(request):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, repos, author: c.get_all_assigned_prs(repos, author=author),
        active_tab='assigned',
        tab_changed='assigned',
    )


@login_required
def repo_review_requests_list(request, owner, repo):
    my_review = request.GET.get('my_review', '')
    fetch_params = _get_review_fetch_params(my_review)

    def make_post_filter(client):
        def post_filter(prs, username):
            if my_review == 'approved':
                filtered = client._filter_prs_approved_by_user(prs, username)
            elif my_review == 'reviewed':
                filtered = client._filter_prs_reviewed_not_approved_by_user(prs, username)
            else:
                filtered = prs
            # Always exclude own PRs from review requests
            return [pr for pr in filtered if pr.author != username]
        return post_filter

    return _pr_list_view(
        request,
        fetch_prs=lambda c, o, r, author: c.get_review_requests_for_repo(
            o, r, author=author, **fetch_params
        ),
        active_tab='review_requests',
        tab_changed='review_requests',
        owner=owner,
        repo=repo,
        post_filter_factory=make_post_filter,
    )


@login_required
def repo_review_approved_list(request, owner, repo):
    def make_post_filter(client):
        def filter_approved_not_own(prs, username):
            filtered = client._filter_prs_approved_by_user(prs, username)
            return [pr for pr in filtered if pr.author != username]
        return filter_approved_not_own

    return _pr_list_view(
        request,
        fetch_prs=lambda c, o, r, author: c.get_review_requests_for_repo(o, r, approved_by_me=True, author=author),
        active_tab='review_requests',
        tab_changed='review_approved',
        review_tab='approved',
        owner=owner,
        repo=repo,
        post_filter_factory=make_post_filter,
    )


@login_required
def repo_review_reviewed_list(request, owner, repo):
    def make_post_filter(client):
        def filter_reviewed_not_own(prs, username):
            filtered = client._filter_prs_reviewed_not_approved_by_user(prs, username)
            return [pr for pr in filtered if pr.author != username]
        return filter_reviewed_not_own

    return _pr_list_view(
        request,
        fetch_prs=lambda c, o, r, author: c.get_review_requests_for_repo(o, r, reviewed_by_me=True, author=author),
        active_tab='review_requests',
        tab_changed='review_reviewed',
        review_tab='reviewed',
        owner=owner,
        repo=repo,
        post_filter_factory=make_post_filter,
    )


@login_required
def repo_assigned_list(request, owner, repo):
    return _pr_list_view(
        request,
        fetch_prs=lambda c, o, r, author: c.get_assigned_prs_for_repo(o, r, author=author),
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
    auto_refresh_my_prs = request.POST.get('auto_refresh_my_prs') == 'on'
    auto_refresh_review_requests = request.POST.get('auto_refresh_review_requests') == 'on'
    auto_refresh_assigned = request.POST.get('auto_refresh_assigned') == 'on'
    try:
        auto_refresh_interval = int(request.POST.get('auto_refresh_interval', 5))
    except (ValueError, TypeError):
        auto_refresh_interval = 5

    valid_intervals = [choice[0] for choice in UserPreferences._meta.get_field('auto_refresh_interval').choices]
    if auto_refresh_interval not in valid_intervals:
        auto_refresh_interval = 5

    prefs, _ = UserPreferences.objects.update_or_create(
        user=request.user,
        defaults={
            'auto_refresh_my_prs': auto_refresh_my_prs,
            'auto_refresh_review_requests': auto_refresh_review_requests,
            'auto_refresh_assigned': auto_refresh_assigned,
            'auto_refresh_interval': auto_refresh_interval,
        }
    )

    context = {'prefs': prefs, 'success': True}
    return render(request, 'dashboard/partials/_preferences_form.html', context)
