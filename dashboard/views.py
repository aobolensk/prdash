from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.views.decorators.http import require_POST

from .models import TrackedRepository
from .github_client import GitHubClient


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
    prs = client.get_all_user_prs(repo_tuples)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': None,
        'active_tab': 'open',
    }

    if request.headers.get('HX-Request') == 'true':
        return render(request, 'dashboard/partials/_pr_content.html', context)

    return render(request, 'dashboard/pr_list.html', context)


@login_required
def merged_pr_list(request):
    """Show all merged PRs across all tracked repositories."""
    repos = TrackedRepository.objects.filter(user=request.user)
    repo_tuples = [(repo.owner, repo.name) for repo in repos]

    client = GitHubClient(request.user)
    prs = client.get_all_merged_prs(repo_tuples)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': None,
        'active_tab': 'merged',
    }

    if request.headers.get('HX-Request') == 'true':
        return render(request, 'dashboard/partials/_pr_content.html', context)

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
    prs = client.get_user_prs_for_repo(owner, repo)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': current_repo,
        'active_tab': 'open',
    }

    if request.headers.get('HX-Request') == 'true':
        return render(request, 'dashboard/partials/_pr_content.html', context)

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
    prs = client.get_merged_prs_for_repo(owner, repo)

    context = {
        'prs': prs,
        'repos': repos,
        'current_repo': current_repo,
        'active_tab': 'merged',
    }

    if request.headers.get('HX-Request') == 'true':
        return render(request, 'dashboard/partials/_pr_content.html', context)

    return render(request, 'dashboard/pr_list.html', context)


@login_required
@require_POST
def add_repo(request):
    """Add a new repository to track."""
    repo_input = request.POST.get('repo', '').strip()

    if '/' not in repo_input:
        return HttpResponse(
            '<div class="error">Invalid format. Use owner/repo</div>',
            status=400
        )

    parts = repo_input.split('/', 1)
    owner, name = parts[0].strip(), parts[1].strip()

    if not owner or not name:
        return HttpResponse(
            '<div class="error">Invalid format. Use owner/repo</div>',
            status=400
        )

    # Validate the repository exists
    client = GitHubClient(request.user)
    valid, message = client.validate_repo(owner, name)

    if not valid:
        return HttpResponse(
            f'<div class="error">{message}</div>',
            status=400
        )

    # Create or get the repository
    repo, created = TrackedRepository.objects.get_or_create(
        user=request.user,
        owner=owner,
        name=name
    )

    if not created:
        return HttpResponse(
            '<div class="error">Repository already tracked</div>',
            status=400
        )

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
