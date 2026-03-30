from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.views.decorators.http import require_POST
import json
import re

from .models import TrackedRepository, PersonalAccessToken
from .github_client import GitHubClient


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
        name = parts[1].strip().rstrip('.git')
        return owner, name

    return None, None


def home(request):
    """Landing page - redirect to PRs if logged in."""
    if request.user.is_authenticated:
        return redirect('dashboard:pr_list')
    return render(request, 'dashboard/home.html')


@login_required
def pr_list(request):
    """Show all PRs across all tracked repositories."""
    repos = TrackedRepository.objects.filter(user=request.user)
    repo_tuples = [(repo.owner, repo.name) for repo in repos]

    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    prs = client.get_all_user_prs(repo_tuples, author=author)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': None,
        'active_tab': 'open',
        'author': author,
        'current_username': current_username,
    }

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': 'my_prs', 'repoChanged': '', 'reviewTabChanged': 'pending'}
        if client.errors:
            triggers['showErrors'] = client.errors
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def merged_pr_list(request):
    """Show all merged PRs across all tracked repositories."""
    repos = TrackedRepository.objects.filter(user=request.user)
    repo_tuples = [(repo.owner, repo.name) for repo in repos]

    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    prs = client.get_all_merged_prs(repo_tuples, author=author)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': None,
        'active_tab': 'merged',
        'author': author,
        'current_username': current_username,
    }

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': 'merged', 'repoChanged': '', 'reviewTabChanged': 'pending'}
        if client.errors:
            triggers['showErrors'] = client.errors
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def repo_pr_list(request, owner, repo):
    """Show PRs for a specific repository."""
    repos = TrackedRepository.objects.filter(user=request.user)
    current_repo = get_object_or_404(
        TrackedRepository,
        user=request.user,
        owner=owner,
        name=repo
    )

    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    prs = client.get_user_prs_for_repo(owner, repo, author=author)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': current_repo,
        'active_tab': 'open',
        'author': author,
        'current_username': current_username,
    }

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': 'my_prs', 'repoChanged': f'{owner}/{repo}', 'reviewTabChanged': 'pending'}
        if client.errors:
            triggers['showErrors'] = client.errors
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def repo_merged_pr_list(request, owner, repo):
    """Show merged PRs for a specific repository."""
    repos = TrackedRepository.objects.filter(user=request.user)
    current_repo = get_object_or_404(
        TrackedRepository,
        user=request.user,
        owner=owner,
        name=repo
    )

    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    prs = client.get_merged_prs_for_repo(owner, repo, author=author)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': current_repo,
        'active_tab': 'merged',
        'author': author,
        'current_username': current_username,
    }

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': 'merged', 'repoChanged': f'{owner}/{repo}', 'reviewTabChanged': 'pending'}
        if client.errors:
            triggers['showErrors'] = client.errors
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def review_requests_list(request):
    """Show all PRs where the current user's review is requested (pending review)."""
    repos = TrackedRepository.objects.filter(user=request.user)
    repo_tuples = [(repo.owner, repo.name) for repo in repos]

    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    prs = client.get_all_review_requests(repo_tuples, approved_by_me=False, author=author)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': None,
        'active_tab': 'review_requests',
        'review_tab': 'pending',
        'author': author,
        'current_username': current_username,
    }

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': 'review_requests', 'repoChanged': '', 'reviewTabChanged': 'pending'}
        if client.errors:
            triggers['showErrors'] = client.errors
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def review_approved_list(request):
    """Show all PRs that the current user has approved."""
    repos = TrackedRepository.objects.filter(user=request.user)
    repo_tuples = [(repo.owner, repo.name) for repo in repos]

    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    prs = client.get_all_review_requests(repo_tuples, approved_by_me=True, author=author)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': None,
        'active_tab': 'review_requests',
        'review_tab': 'approved',
        'author': author,
        'current_username': current_username,
    }

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': 'review_approved', 'repoChanged': '', 'reviewTabChanged': 'approved'}
        if client.errors:
            triggers['showErrors'] = client.errors
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def review_reviewed_list(request):
    """Show all PRs that the current user has reviewed (but not approved)."""
    repos = TrackedRepository.objects.filter(user=request.user)
    repo_tuples = [(repo.owner, repo.name) for repo in repos]

    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    prs = client.get_all_review_requests(repo_tuples, reviewed_by_me=True, author=author)
    # Exclude the current user's own PRs
    if current_username:
        prs = [pr for pr in prs if pr.author != current_username]

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': None,
        'active_tab': 'review_requests',
        'review_tab': 'reviewed',
        'author': author,
        'current_username': current_username,
    }

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': 'review_reviewed', 'repoChanged': '', 'reviewTabChanged': 'reviewed'}
        if client.errors:
            triggers['showErrors'] = client.errors
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def assigned_list(request):
    """Show all PRs where the current user is assigned."""
    repos = TrackedRepository.objects.filter(user=request.user)
    repo_tuples = [(repo.owner, repo.name) for repo in repos]

    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    prs = client.get_all_assigned_prs(repo_tuples, author=author)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': None,
        'active_tab': 'assigned',
        'author': author,
        'current_username': current_username,
    }

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': 'assigned', 'repoChanged': '', 'reviewTabChanged': 'pending'}
        if client.errors:
            triggers['showErrors'] = client.errors
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def repo_review_requests_list(request, owner, repo):
    """Show PRs where the current user's review is requested for a specific repository."""
    repos = TrackedRepository.objects.filter(user=request.user)
    current_repo = get_object_or_404(
        TrackedRepository,
        user=request.user,
        owner=owner,
        name=repo
    )

    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    prs = client.get_review_requests_for_repo(owner, repo, approved_by_me=False, author=author)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': current_repo,
        'active_tab': 'review_requests',
        'review_tab': 'pending',
        'author': author,
        'current_username': current_username,
    }

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': 'review_requests', 'repoChanged': f'{owner}/{repo}', 'reviewTabChanged': 'pending'}
        if client.errors:
            triggers['showErrors'] = client.errors
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def repo_review_approved_list(request, owner, repo):
    """Show PRs that the current user has approved for a specific repository."""
    repos = TrackedRepository.objects.filter(user=request.user)
    current_repo = get_object_or_404(
        TrackedRepository,
        user=request.user,
        owner=owner,
        name=repo
    )

    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    prs = client.get_review_requests_for_repo(owner, repo, approved_by_me=True, author=author)
    # Filter to only PRs approved by the user
    username = client.get_username()
    if username:
        prs = client._filter_prs_approved_by_user(prs, username)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': current_repo,
        'active_tab': 'review_requests',
        'review_tab': 'approved',
        'author': author,
        'current_username': current_username,
    }

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': 'review_approved', 'repoChanged': f'{owner}/{repo}', 'reviewTabChanged': 'approved'}
        if client.errors:
            triggers['showErrors'] = client.errors
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def repo_review_reviewed_list(request, owner, repo):
    """Show PRs that the current user has reviewed (but not approved) for a specific repository."""
    repos = TrackedRepository.objects.filter(user=request.user)
    current_repo = get_object_or_404(
        TrackedRepository,
        user=request.user,
        owner=owner,
        name=repo
    )

    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    prs = client.get_review_requests_for_repo(owner, repo, reviewed_by_me=True, author=author)
    # Filter to only PRs reviewed (but not approved) by the user
    if current_username:
        prs = client._filter_prs_reviewed_not_approved_by_user(prs, current_username)
        # Exclude the current user's own PRs
        prs = [pr for pr in prs if pr.author != current_username]

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': current_repo,
        'active_tab': 'review_requests',
        'review_tab': 'reviewed',
        'author': author,
        'current_username': current_username,
    }

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': 'review_reviewed', 'repoChanged': f'{owner}/{repo}', 'reviewTabChanged': 'reviewed'}
        if client.errors:
            triggers['showErrors'] = client.errors
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def repo_assigned_list(request, owner, repo):
    """Show PRs where the current user is assigned for a specific repository."""
    repos = TrackedRepository.objects.filter(user=request.user)
    current_repo = get_object_or_404(
        TrackedRepository,
        user=request.user,
        owner=owner,
        name=repo
    )

    client = GitHubClient(request.user)
    author = request.GET.get('author', '').strip() or None
    current_username = client.get_username()
    prs = client.get_assigned_prs_for_repo(owner, repo, author=author)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': current_repo,
        'active_tab': 'assigned',
        'author': author,
        'current_username': current_username,
    }

    if request.headers.get('HX-Request') == 'true':
        response = render(request, 'dashboard/partials/_pr_content.html', context)
        triggers = {'tabChanged': 'assigned', 'repoChanged': f'{owner}/{repo}', 'reviewTabChanged': 'pending'}
        if client.errors:
            triggers['showErrors'] = client.errors
        response['HX-Trigger'] = json.dumps(triggers)
        return response

    return render(request, 'dashboard/pr_list.html', context)


@login_required
@require_POST
def add_repo(request):
    """Add a new repository to track."""
    repo_input = request.POST.get('repo', '').strip()

    # Extract owner/repo from various formats
    owner, name = _parse_repo_input(repo_input)

    if not owner or not name:
        repos = TrackedRepository.objects.filter(user=request.user)
        response = render(request, 'dashboard/partials/_repo_list.html', {'repos': repos})
        response['HX-Trigger'] = json.dumps({'showErrors': ['Invalid format. Use owner/repo']})
        return response

    # Validate the repository exists
    client = GitHubClient(request.user)
    valid, message = client.validate_repo(owner, name)

    if not valid:
        repos = TrackedRepository.objects.filter(user=request.user)
        response = render(request, 'dashboard/partials/_repo_list.html', {'repos': repos})
        response['HX-Trigger'] = json.dumps({'showErrors': [message]})
        return response

    # Create or get the repository
    repo, created = TrackedRepository.objects.get_or_create(
        user=request.user,
        owner=owner,
        name=name
    )

    if not created:
        repos = TrackedRepository.objects.filter(user=request.user)
        response = render(request, 'dashboard/partials/_repo_list.html', {'repos': repos})
        response['HX-Trigger'] = json.dumps({'showErrors': ['Repository already tracked']})
        return response

    repos = TrackedRepository.objects.filter(user=request.user)
    response = render(request, 'dashboard/partials/_repo_list.html', {'repos': repos})
    response['HX-Trigger'] = 'repoAdded'
    return response


@login_required
@require_POST
def remove_repo(request, repo_id):
    """Remove a tracked repository."""
    repo = get_object_or_404(TrackedRepository, id=repo_id, user=request.user)
    repo.delete()

    repos = TrackedRepository.objects.filter(user=request.user)
    response = render(request, 'dashboard/partials/_repo_list.html', {'repos': repos})
    response['HX-Trigger'] = 'repoRemoved'
    return response


@login_required
def settings(request):
    """User settings page."""
    pat = PersonalAccessToken.objects.filter(user=request.user).first()
    context = {
        'pat': pat,
    }
    return render(request, 'dashboard/settings.html', context)


@login_required
@require_POST
def save_pat(request):
    """Save or update Personal Access Token."""
    token = request.POST.get('token', '').strip()

    if not token:
        # Delete existing PAT if empty token submitted
        PersonalAccessToken.objects.filter(user=request.user).delete()
        pat = None
    else:
        # Validate the token by making a test API call
        from github import Github
        try:
            g = Github(token, timeout=10)
            user = g.get_user()
            _ = user.login  # Force API call to validate token
        except Exception as e:
            pat = PersonalAccessToken.objects.filter(user=request.user).first()
            context = {'pat': pat, 'error': f'Invalid token: {str(e)}'}
            return render(request, 'dashboard/partials/_pat_form.html', context)

        # Save or update the PAT
        pat, created = PersonalAccessToken.objects.update_or_create(
            user=request.user,
            defaults={'token': token}
        )

    context = {'pat': pat, 'success': True}
    return render(request, 'dashboard/partials/_pat_form.html', context)


@login_required
@require_POST
def delete_pat(request):
    """Delete Personal Access Token."""
    PersonalAccessToken.objects.filter(user=request.user).delete()
    context = {'pat': None, 'deleted': True}
    return render(request, 'dashboard/partials/_pat_form.html', context)
