"""GitHub API client for fetching PR information."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.cache import cache

if TYPE_CHECKING:
    from github import Github, GithubException


@dataclass
class CIStatus:
    """Represents the CI status of a pull request."""
    state: str  # 'success', 'pending', 'failure', 'error', or 'unknown'
    passed_count: int = 0
    total_count: int = 0
    context: Optional[str] = None
    description: Optional[str] = None
    target_url: Optional[str] = None


@dataclass
class ReviewStatus:
    """Represents the review status of a pull request."""
    state: str  # 'approved', 'changes_requested', 'not_reviewed'
    approval_count: int = 0
    comment_count: int = 0


@dataclass
class PullRequestInfo:
    """Represents a pull request with relevant information."""
    number: int
    title: str
    url: str
    repo_owner: str
    repo_name: str
    author: str
    author_avatar: str
    created_at: datetime
    updated_at: datetime
    labels: list[dict]
    ci_status: CIStatus
    review_status: ReviewStatus
    draft: bool
    additions: int
    deletions: int
    branch_name: str = ""
    head_repo_owner: str = ""
    head_repo_name: str = ""
    mergeable: Optional[str] = None
    merged_at: Optional[datetime] = None

    @property
    def repo_full_name(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"


class GitHubClient:
    """Client for interacting with the GitHub API."""

    def __init__(self, user):
        self.user = user
        self._client = None
        self.errors = []

    @property
    def client(self):
        """Lazily initialize the GitHub client with the user's OAuth token."""
        if self._client is None:
            token = self._get_token()
            if token:
                from github import Github
                # Add timeout of 10 seconds to prevent hanging
                self._client = Github(token, timeout=10)
        return self._client

    def _get_token(self) -> Optional[str]:
        """Get the GitHub token for the user.

        Prefers Personal Access Token (PAT) if available, falls back to OAuth token.
        PAT is useful for accessing enterprise repos that require SSO authorization.
        """
        # First, try to get a Personal Access Token (preferred for enterprise repos)
        from .models import PersonalAccessToken
        try:
            pat = PersonalAccessToken.objects.get(user=self.user)
            if pat.token:
                return pat.token
        except PersonalAccessToken.DoesNotExist:
            pass

        # Fall back to OAuth token
        from allauth.socialaccount.models import SocialToken
        try:
            social_token = SocialToken.objects.filter(
                account__user=self.user,
                account__provider='github'
            ).first()
            if social_token:
                return social_token.token
        except SocialToken.DoesNotExist:
            pass
        return None

    def _get_ci_status(self, pr) -> CIStatus:
        """Get the combined CI status for a pull request with job counts."""
        try:
            commit = pr.get_commits().reversed[0]
            combined_status = commit.get_combined_status()
            check_runs = commit.get_check_runs()

            total_count = 0
            passed_count = 0
            state = 'unknown'

            # Count check runs (GitHub Actions)
            if check_runs.totalCount > 0:
                total_count = check_runs.totalCount
                success_count = 0
                failure_count = 0
                skipped_count = 0
                pending_count = 0

                for cr in check_runs:
                    if cr.conclusion == 'success':
                        success_count += 1
                        passed_count += 1
                    elif cr.conclusion in ('failure', 'cancelled', 'timed_out'):
                        failure_count += 1
                    elif cr.conclusion in ('skipped', 'neutral'):
                        skipped_count += 1
                    elif cr.status in ('queued', 'in_progress'):
                        pending_count += 1

                # Determine overall state
                # If any failures, state is failure
                if failure_count > 0:
                    state = 'failure'
                # If any pending/in-progress, state is pending
                elif pending_count > 0:
                    state = 'pending'
                # If we have successes and no failures (skipped is OK), it's success
                elif success_count > 0:
                    state = 'success'
                # All skipped/neutral
                elif skipped_count == total_count:
                    state = 'success'  # All skipped is still a passing state
                else:
                    state = 'unknown'

            # Also count commit statuses (legacy CI systems)
            elif combined_status.total_count > 0:
                total_count = combined_status.total_count
                state = combined_status.state

                for status in combined_status.statuses:
                    if status.state == 'success':
                        passed_count += 1

            return CIStatus(
                state=state,
                passed_count=passed_count,
                total_count=total_count
            )
        except Exception as e:
            return CIStatus(state='unknown', passed_count=0, total_count=0)

    def _get_review_status(self, pr) -> ReviewStatus:
        """Get the review status for a pull request."""
        try:
            reviews = pr.get_reviews()
            comment_count = pr.comments + pr.review_comments

            # Track latest review state per user (only count the most recent review from each user)
            # Note: COMMENTED reviews don't override APPROVED/CHANGES_REQUESTED
            latest_review_by_user = {}
            for review in reviews:
                if review.state in ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED'):
                    user = review.user.login
                    # Only update if:
                    # 1. User hasn't reviewed yet, OR
                    # 2. New review is later AND (new is APPROVED/CHANGES_REQUESTED, or old was just COMMENTED)
                    if user not in latest_review_by_user:
                        latest_review_by_user[user] = (review.state, review.submitted_at)
                    elif review.submitted_at > latest_review_by_user[user][1]:
                        old_state = latest_review_by_user[user][0]
                        # Only override if new state is "stronger" or old state was just COMMENTED
                        if review.state in ('APPROVED', 'CHANGES_REQUESTED') or old_state == 'COMMENTED':
                            latest_review_by_user[user] = (review.state, review.submitted_at)

            approval_count = sum(1 for state, _ in latest_review_by_user.values() if state == 'APPROVED')
            changes_requested = any(state == 'CHANGES_REQUESTED' for state, _ in latest_review_by_user.values())

            if changes_requested:
                state = 'changes_requested'
            elif approval_count > 0:
                state = 'approved'
            else:
                state = 'not_reviewed'

            return ReviewStatus(
                state=state,
                approval_count=approval_count,
                comment_count=comment_count
            )
        except Exception as e:
            return ReviewStatus(state='not_reviewed', approval_count=0, comment_count=0)

    def validate_repo(self, owner: str, name: str) -> tuple[bool, str]:
        """Validate that a repository exists and is accessible."""
        if not self.client:
            return False, "Not authenticated with GitHub"

        try:
            from github import GithubException
            repo = self.client.get_repo(f"{owner}/{name}")
            return True, f"Found: {repo.full_name}"
        except Exception as e:
            # Handle GithubException without importing at module level
            if hasattr(e, 'status'):
                # Extract detailed message if available
                detailed_msg = ""
                if hasattr(e, 'data') and isinstance(e.data, dict):
                    detailed_msg = e.data.get('message', '')

                if e.status == 404:
                    error_msg = f"Repository {owner}/{name} not found"
                elif e.status == 403:
                    if 'SAML' in detailed_msg:
                        error_msg = f"Access denied to {owner}/{name}: SAML SSO enforcement. Re-authorize OAuth app."
                    elif detailed_msg:
                        error_msg = f"Access denied to {owner}/{name}: {detailed_msg}"
                    else:
                        error_msg = f"Access denied to {owner}/{name} (you may not have permission)"
                else:
                    error_msg = f"Error accessing {owner}/{name}: {detailed_msg}" if detailed_msg else f"Error accessing {owner}/{name}"
                return False, error_msg
            return False, f"Error accessing {owner}/{name}: {str(e)}"

    def _fetch_pr_details(self, pr_number: int, owner: str, name: str) -> Optional[PullRequestInfo]:
        """Fetch full PR details including CI status for a single PR."""
        try:
            repo = self.client.get_repo(f"{owner}/{name}")
            pr = repo.get_pull(pr_number)
            return self._pr_to_info(pr, owner, name)
        except Exception as e:
            print(f"ERROR: Failed to fetch PR #{pr_number}: {e}")
            return None

    def _fetch_prs_batch_graphql(self, owner: str, name: str, pr_numbers: list[int]) -> list[PullRequestInfo]:
        """Fetch multiple PRs using GraphQL to minimize API calls."""
        if not pr_numbers:
            return []

        try:
            # Build GraphQL query to fetch all PR data at once
            # This reduces API calls from N (one per PR) to 1 for up to 100 PRs
            pr_queries = []
            for i, pr_num in enumerate(pr_numbers[:100]):  # GraphQL has query complexity limits
                pr_queries.append(f'''
                    pr{i}: pullRequest(number: {pr_num}) {{
                        number
                        title
                        url
                        author {{
                            login
                            avatarUrl
                        }}
                        createdAt
                        updatedAt
                        mergedAt
                        isDraft
                        additions
                        deletions
                        mergeable
                        headRefName
                        headRepository {{
                            owner {{
                                login
                            }}
                            name
                        }}
                        labels(first: 20) {{
                            nodes {{
                                name
                                color
                            }}
                        }}
                        commits(last: 1) {{
                            nodes {{
                                commit {{
                                    statusCheckRollup {{
                                        state
                                        contexts(first: 100) {{
                                            totalCount
                                            nodes {{
                                                ... on CheckRun {{
                                                    name
                                                    status
                                                    conclusion
                                                }}
                                                ... on StatusContext {{
                                                    state
                                                    context
                                                }}
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                        reviews(first: 100) {{
                            nodes {{
                                author {{
                                    login
                                }}
                                state
                                submittedAt
                            }}
                        }}
                        comments {{
                            totalCount
                        }}
                        reviewThreads {{
                            totalCount
                        }}
                    }}
                ''')

            query = f'''
                query {{
                    repository(owner: "{owner}", name: "{name}") {{
                        {' '.join(pr_queries)}
                    }}
                }}
            '''

            # Execute GraphQL query
            token = self._get_token()
            if not token:
                return []

            import requests
            response = requests.post(
                'https://api.github.com/graphql',
                json={'query': query},
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                },
                timeout=30
            )

            if response.status_code != 200:
                print(f"GraphQL query failed: {response.status_code} - {response.text}")
                return []

            data = response.json()
            if 'errors' in data:
                print(f"GraphQL errors: {data['errors']}")
                # Collect GraphQL errors
                for error in data['errors']:
                    if error.get('type') == 'NOT_FOUND':
                        error_msg = f"Repository {owner}/{name} not found or not accessible"
                        self.errors.append(error_msg)

            # Parse results
            result = []
            repo_data = data.get('data', {}).get('repository', {})

            if not repo_data:
                print(f"ERROR: No repository data in GraphQL response")
                return []

            for i, pr_num in enumerate(pr_numbers[:100]):
                pr_data = repo_data.get(f'pr{i}')
                if not pr_data:
                    continue

                # Parse labels
                labels = [
                    {'name': label['name'], 'color': label['color']}
                    for label in pr_data.get('labels', {}).get('nodes', [])
                ]

                # Parse CI status
                ci_status = self._parse_ci_status_from_graphql(pr_data)

                # Parse review status
                review_status = self._parse_review_status_from_graphql(pr_data)

                merged_at = None
                if pr_data.get('mergedAt'):
                    merged_at = datetime.fromisoformat(pr_data['mergedAt'].replace('Z', '+00:00'))

                mergeable = pr_data.get('mergeable')
                cache_key = f"pr_mergeable:{owner}/{name}:{pr_data['number']}"
                if mergeable and mergeable != 'UNKNOWN':
                    cache.set(cache_key, mergeable, 3600)
                elif mergeable is None or mergeable == 'UNKNOWN':
                    mergeable = cache.get(cache_key)

                # Extract head repository info (could be a fork)
                head_repo = pr_data.get('headRepository')
                head_repo_owner = owner
                head_repo_name = name
                if head_repo and head_repo.get('owner'):
                    head_repo_owner = head_repo['owner']['login']
                    head_repo_name = head_repo['name']

                pr_info = PullRequestInfo(
                    number=pr_data['number'],
                    title=pr_data['title'],
                    url=pr_data['url'],
                    repo_owner=owner,
                    repo_name=name,
                    author=pr_data['author']['login'],
                    author_avatar=pr_data['author']['avatarUrl'],
                    created_at=datetime.fromisoformat(pr_data['createdAt'].replace('Z', '+00:00')),
                    updated_at=datetime.fromisoformat(pr_data['updatedAt'].replace('Z', '+00:00')),
                    labels=labels,
                    ci_status=ci_status,
                    review_status=review_status,
                    draft=pr_data.get('isDraft', False),
                    additions=pr_data.get('additions', 0),
                    deletions=pr_data.get('deletions', 0),
                    branch_name=pr_data.get('headRefName', ''),
                    head_repo_owner=head_repo_owner,
                    head_repo_name=head_repo_name,
                    mergeable=mergeable,
                    merged_at=merged_at,
                )
                result.append(pr_info)

            return result

        except Exception as e:
            print(f"ERROR: Batch GraphQL fetch failed: {e}")
            return []

    def _parse_ci_status_from_graphql(self, pr_data: dict) -> CIStatus:
        """Parse CI status from GraphQL response."""
        try:
            commits = pr_data.get('commits', {}).get('nodes', [])
            if not commits:
                return CIStatus(state='unknown')

            rollup = commits[0].get('commit', {}).get('statusCheckRollup')
            if not rollup:
                return CIStatus(state='unknown')

            contexts_data = rollup.get('contexts', {})
            total_count = contexts_data.get('totalCount', 0)
            contexts = contexts_data.get('nodes', [])

            if total_count == 0:
                return CIStatus(state='unknown')

            # Count check runs by conclusion/state (matching REST API logic)
            success_count = 0
            failure_count = 0
            skipped_count = 0
            pending_count = 0

            for context in contexts:
                # Check if it's a CheckRun or StatusContext
                if 'conclusion' in context:  # CheckRun
                    conclusion = context.get('conclusion')
                    status = context.get('status')

                    if conclusion == 'SUCCESS':
                        success_count += 1
                    elif conclusion in ('FAILURE', 'CANCELLED', 'TIMED_OUT'):
                        failure_count += 1
                    elif conclusion in ('SKIPPED', 'NEUTRAL', 'STALE'):
                        skipped_count += 1
                    elif conclusion is None and status in ('QUEUED', 'IN_PROGRESS'):
                        # If no conclusion yet, check status
                        pending_count += 1

                elif 'state' in context:  # StatusContext
                    state_value = context.get('state')
                    if state_value == 'SUCCESS':
                        success_count += 1
                    elif state_value in ('FAILURE', 'ERROR'):
                        failure_count += 1
                    elif state_value == 'PENDING':
                        pending_count += 1

            # Determine overall state (matching REST API logic)
            if failure_count > 0:
                state = 'failure'
            elif pending_count > 0:
                state = 'pending'
            elif success_count > 0:
                state = 'success'
            elif skipped_count == total_count:
                state = 'success'
            else:
                state = 'unknown'

            return CIStatus(
                state=state,
                passed_count=success_count,
                total_count=total_count
            )
        except Exception as e:
            return CIStatus(state='unknown')

    def _parse_review_status_from_graphql(self, pr_data: dict) -> ReviewStatus:
        """Parse review status from GraphQL response."""
        try:
            reviews = pr_data.get('reviews', {}).get('nodes', [])
            comment_count = pr_data.get('comments', {}).get('totalCount', 0)
            comment_count += pr_data.get('reviewThreads', {}).get('totalCount', 0)

            # Track latest review state per user
            # Note: COMMENTED reviews don't override APPROVED/CHANGES_REQUESTED
            latest_review_by_user = {}
            for review in reviews:
                if not review.get('author'):
                    continue
                user = review['author']['login']
                state = review.get('state', '')
                submitted_at = review.get('submittedAt')

                if state in ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED'):
                    # Only update if:
                    # 1. User hasn't reviewed yet, OR
                    # 2. New review is later AND (new is APPROVED/CHANGES_REQUESTED, or old was just COMMENTED)
                    if user not in latest_review_by_user:
                        latest_review_by_user[user] = (state, submitted_at)
                    elif submitted_at > latest_review_by_user[user][1]:
                        old_state = latest_review_by_user[user][0]
                        # Only override if new state is "stronger" or old state was just COMMENTED
                        if state in ('APPROVED', 'CHANGES_REQUESTED') or old_state == 'COMMENTED':
                            latest_review_by_user[user] = (state, submitted_at)

            approval_count = sum(1 for state, _ in latest_review_by_user.values() if state == 'APPROVED')
            changes_requested = any(state == 'CHANGES_REQUESTED' for state, _ in latest_review_by_user.values())

            if changes_requested:
                state = 'changes_requested'
            elif approval_count > 0:
                state = 'approved'
            else:
                state = 'not_reviewed'

            return ReviewStatus(
                state=state,
                approval_count=approval_count,
                comment_count=comment_count
            )
        except Exception as e:
            return ReviewStatus(state='not_reviewed', approval_count=0, comment_count=0)

    def get_username(self) -> Optional[str]:
        """Get the authenticated user's GitHub username."""
        if not self.client:
            return None
        try:
            return self.client.get_user().login
        except Exception:
            return None

    def get_user_prs_for_repo(self, owner: str, name: str, author: Optional[str] = None) -> list[PullRequestInfo]:
        """Get open PRs authored by the specified user (or current user) for a specific repository."""
        if not self.client:
            return []

        try:
            # Use provided author or default to authenticated user
            if author is None:
                github_user = self.client.get_user()
                author = github_user.login

            # Use GitHub Search API to filter PRs by author - much more efficient!
            query = f"repo:{owner}/{name} is:pr is:open author:{author}"
            issues = self.client.search_issues(query, sort='updated', order='desc')

            # Collect PR numbers
            pr_numbers = [issue.number for issue in issues]

            if not pr_numbers:
                return []

            result = self._fetch_prs_batch_graphql(owner, name, pr_numbers)

            result.sort(key=lambda pr: (pr.updated_at, pr.number), reverse=True)
            return result
        except Exception as e:
            print(f"ERROR: Failed to get PRs for {owner}/{name}: {e}")
            return []

    def get_all_user_prs(self, repos: list[tuple[str, str]], author: Optional[str] = None) -> list[PullRequestInfo]:
        """Get all open PRs authored by the specified user (or current user) across multiple repositories."""
        if not repos:
            return []

        all_prs = []

        # Fetch PRs from all repos in parallel
        with ThreadPoolExecutor(max_workers=min(10, len(repos))) as executor:
            # Submit all repo fetch tasks
            future_to_repo = {
                executor.submit(self.get_user_prs_for_repo, owner, name, author): (owner, name)
                for owner, name in repos
            }

            # Collect results as they complete
            for future in as_completed(future_to_repo):
                repo = future_to_repo[future]
                try:
                    prs = future.result()
                    all_prs.extend(prs)
                except Exception as e:
                    owner, name = repo
                    print(f"ERROR: Failed to fetch PRs for {owner}/{name}: {e}")

        all_prs.sort(key=lambda pr: (pr.updated_at, pr.number), reverse=True)
        return all_prs

    def get_merged_prs_for_repo(self, owner: str, name: str, author: Optional[str] = None) -> list[PullRequestInfo]:
        """Get recently merged PRs authored by the specified user (or current user) for a specific repository."""
        if not self.client:
            return []

        try:
            # Use provided author or default to authenticated user
            if author is None:
                github_user = self.client.get_user()
                author = github_user.login

            # Use GitHub Search API to find merged PRs by author
            query = f"repo:{owner}/{name} is:pr is:merged author:{author}"
            issues = self.client.search_issues(query, sort='updated', order='desc')

            # Collect PR numbers (limit to 50 most recent)
            pr_numbers = [issue.number for issue in issues][:50]

            if not pr_numbers:
                return []

            result = self._fetch_prs_batch_graphql(owner, name, pr_numbers)

            result.sort(key=lambda pr: (pr.merged_at or pr.updated_at, pr.number), reverse=True)
            return result
        except Exception as e:
            print(f"ERROR: Failed to get merged PRs for {owner}/{name}: {e}")
            return []

    def get_all_merged_prs(self, repos: list[tuple[str, str]], author: Optional[str] = None) -> list[PullRequestInfo]:
        """Get all recently merged PRs authored by the specified user (or current user) across multiple repositories."""
        if not repos:
            return []

        all_prs = []

        # Fetch PRs from all repos in parallel
        with ThreadPoolExecutor(max_workers=min(10, len(repos))) as executor:
            future_to_repo = {
                executor.submit(self.get_merged_prs_for_repo, owner, name, author): (owner, name)
                for owner, name in repos
            }

            for future in as_completed(future_to_repo):
                repo = future_to_repo[future]
                try:
                    prs = future.result()
                    all_prs.extend(prs)
                except Exception as e:
                    owner, name = repo
                    print(f"ERROR: Failed to fetch merged PRs for {owner}/{name}: {e}")

        all_prs.sort(key=lambda pr: (pr.merged_at or pr.updated_at, pr.number), reverse=True)
        return all_prs

    def get_review_requests_for_repo(self, owner: str, name: str, approved_by_me: bool = False, reviewed_by_me: bool = False, author: Optional[str] = None) -> list[PullRequestInfo]:
        """Get open PRs where the current user's review is requested for a specific repository.

        Args:
            approved_by_me: If True, only return PRs that I have approved. If False, return PRs pending my review.
            reviewed_by_me: If True, return PRs that I have reviewed (any review state).
            author: If provided, filter PRs to only include those authored by this user.
        """
        if not self.client:
            return []

        try:
            github_user = self.client.get_user()
            username = github_user.login

            if approved_by_me or reviewed_by_me:
                # Search for PRs where I was a reviewer
                query = f"repo:{owner}/{name} is:pr is:open reviewed-by:{username}"
            else:
                # Search for PRs where review is requested from me
                query = f"repo:{owner}/{name} is:pr is:open review-requested:{username}"

            # Add author filter if provided
            if author:
                query += f" author:{author}"

            issues = self.client.search_issues(query, sort='updated', order='desc')

            # Collect PR numbers
            pr_numbers = [issue.number for issue in issues]

            if not pr_numbers:
                return []

            result = self._fetch_prs_batch_graphql(owner, name, pr_numbers)

            result.sort(key=lambda pr: (pr.updated_at, pr.number), reverse=True)
            return result
        except Exception as e:
            print(f"ERROR: Failed to get review requests for {owner}/{name}: {e}")
            return []

    def get_all_review_requests(self, repos: list[tuple[str, str]], approved_by_me: bool = False, reviewed_by_me: bool = False, author: Optional[str] = None) -> list[PullRequestInfo]:
        """Get all open PRs where the current user's review is requested across multiple repositories.

        Args:
            approved_by_me: If True, only return PRs that I have approved. If False, return PRs pending my review.
            reviewed_by_me: If True, return PRs that I have reviewed (any review state).
            author: If provided, filter PRs to only include those authored by this user.
        """
        if not repos:
            return []

        all_prs = []
        username = self.get_username()

        # Fetch PRs from all repos in parallel
        with ThreadPoolExecutor(max_workers=min(10, len(repos))) as executor:
            future_to_repo = {
                executor.submit(self.get_review_requests_for_repo, owner, name, approved_by_me, reviewed_by_me, author): (owner, name)
                for owner, name in repos
            }

            for future in as_completed(future_to_repo):
                repo = future_to_repo[future]
                try:
                    prs = future.result()
                    all_prs.extend(prs)
                except Exception as e:
                    owner, name = repo
                    print(f"ERROR: Failed to fetch review requests for {owner}/{name}: {e}")

        # If approved_by_me, we need to filter PRs where we actually approved
        if approved_by_me and username:
            all_prs = self._filter_prs_approved_by_user(all_prs, username)
        # If reviewed_by_me, filter to PRs where user has reviewed but NOT approved
        elif reviewed_by_me and username:
            all_prs = self._filter_prs_reviewed_not_approved_by_user(all_prs, username)

        all_prs.sort(key=lambda pr: (pr.updated_at, pr.number), reverse=True)
        return all_prs

    def get_assigned_prs_for_repo(self, owner: str, name: str, author: Optional[str] = None) -> list[PullRequestInfo]:
        """Get open PRs where the current user is assigned for a specific repository.

        Args:
            author: If provided, filter PRs to only include those authored by this user.
        """
        if not self.client:
            return []

        try:
            github_user = self.client.get_user()
            username = github_user.login

            # Use GitHub Search API to find PRs assigned to the current user
            query = f"repo:{owner}/{name} is:pr is:open assignee:{username}"

            # Add author filter if provided
            if author:
                query += f" author:{author}"

            issues = self.client.search_issues(query, sort='updated', order='desc')

            # Collect PR numbers
            pr_numbers = [issue.number for issue in issues]

            if not pr_numbers:
                return []

            result = self._fetch_prs_batch_graphql(owner, name, pr_numbers)

            result.sort(key=lambda pr: (pr.updated_at, pr.number), reverse=True)
            return result
        except Exception as e:
            print(f"ERROR: Failed to get assigned PRs for {owner}/{name}: {e}")
            return []

    def get_all_assigned_prs(self, repos: list[tuple[str, str]], author: Optional[str] = None) -> list[PullRequestInfo]:
        """Get all open PRs where the current user is assigned across multiple repositories.

        Args:
            author: If provided, filter PRs to only include those authored by this user.
        """
        if not repos:
            return []

        all_prs = []

        # Fetch PRs from all repos in parallel
        with ThreadPoolExecutor(max_workers=min(10, len(repos))) as executor:
            future_to_repo = {
                executor.submit(self.get_assigned_prs_for_repo, owner, name, author): (owner, name)
                for owner, name in repos
            }

            for future in as_completed(future_to_repo):
                repo = future_to_repo[future]
                try:
                    prs = future.result()
                    all_prs.extend(prs)
                except Exception as e:
                    owner, name = repo
                    print(f"ERROR: Failed to fetch assigned PRs for {owner}/{name}: {e}")

        all_prs.sort(key=lambda pr: (pr.updated_at, pr.number), reverse=True)
        return all_prs

    def _filter_prs_approved_by_user(self, prs: list[PullRequestInfo], username: str) -> list[PullRequestInfo]:
        """Filter PRs to only include those approved by the given user using GraphQL."""
        if not prs:
            return []

        # Group PRs by repo
        prs_by_repo = {}
        for pr in prs:
            key = (pr.repo_owner, pr.repo_name)
            if key not in prs_by_repo:
                prs_by_repo[key] = []
            prs_by_repo[key].append(pr)

        approved_prs = []
        token = self._get_token()
        if not token:
            return []

        import requests

        for (owner, name), repo_prs in prs_by_repo.items():
            pr_numbers = [pr.number for pr in repo_prs]

            # Build GraphQL query to check reviews
            pr_queries = []
            for i, pr_num in enumerate(pr_numbers[:100]):
                pr_queries.append(f'''
                    pr{i}: pullRequest(number: {pr_num}) {{
                        number
                        reviews(first: 100) {{
                            nodes {{
                                author {{
                                    login
                                }}
                                state
                                submittedAt
                            }}
                        }}
                    }}
                ''')

            query = f'''
                query {{
                    repository(owner: "{owner}", name: "{name}") {{
                        {' '.join(pr_queries)}
                    }}
                }}
            '''

            try:
                response = requests.post(
                    'https://api.github.com/graphql',
                    json={'query': query},
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Content-Type': 'application/json',
                    },
                    timeout=30
                )

                if response.status_code != 200:
                    continue

                data = response.json()
                repo_data = data.get('data', {}).get('repository', {})

                if not repo_data:
                    continue

                # Check each PR for user's approval
                pr_map = {pr.number: pr for pr in repo_prs}
                for i, pr_num in enumerate(pr_numbers[:100]):
                    pr_data = repo_data.get(f'pr{i}')
                    if not pr_data:
                        continue

                    reviews = pr_data.get('reviews', {}).get('nodes', [])

                    # Track latest review state per user
                    latest_review_by_user = {}
                    for review in reviews:
                        if not review.get('author'):
                            continue
                        user = review['author']['login']
                        state = review.get('state', '')
                        submitted_at = review.get('submittedAt')

                        if state in ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED'):
                            if user not in latest_review_by_user:
                                latest_review_by_user[user] = (state, submitted_at)
                            elif submitted_at > latest_review_by_user[user][1]:
                                old_state = latest_review_by_user[user][0]
                                if state in ('APPROVED', 'CHANGES_REQUESTED') or old_state == 'COMMENTED':
                                    latest_review_by_user[user] = (state, submitted_at)

                    # Check if user has approved
                    if username in latest_review_by_user and latest_review_by_user[username][0] == 'APPROVED':
                        if pr_num in pr_map:
                            approved_prs.append(pr_map[pr_num])

            except Exception as e:
                print(f"ERROR: Failed to filter approved PRs for {owner}/{name}: {e}")

        return approved_prs

    def _filter_prs_reviewed_not_approved_by_user(self, prs: list[PullRequestInfo], username: str) -> list[PullRequestInfo]:
        """Filter PRs to only include those reviewed (but not approved) by the given user using GraphQL."""
        if not prs:
            return []

        # Group PRs by repo
        prs_by_repo = {}
        for pr in prs:
            key = (pr.repo_owner, pr.repo_name)
            if key not in prs_by_repo:
                prs_by_repo[key] = []
            prs_by_repo[key].append(pr)

        reviewed_prs = []
        token = self._get_token()
        if not token:
            return []

        import requests

        for (owner, name), repo_prs in prs_by_repo.items():
            pr_numbers = [pr.number for pr in repo_prs]

            # Build GraphQL query to check reviews
            pr_queries = []
            for i, pr_num in enumerate(pr_numbers[:100]):
                pr_queries.append(f'''
                    pr{i}: pullRequest(number: {pr_num}) {{
                        number
                        reviews(first: 100) {{
                            nodes {{
                                author {{
                                    login
                                }}
                                state
                                submittedAt
                            }}
                        }}
                    }}
                ''')

            query = f'''
                query {{
                    repository(owner: "{owner}", name: "{name}") {{
                        {' '.join(pr_queries)}
                    }}
                }}
            '''

            try:
                response = requests.post(
                    'https://api.github.com/graphql',
                    json={'query': query},
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Content-Type': 'application/json',
                    },
                    timeout=30
                )

                if response.status_code != 200:
                    continue

                data = response.json()
                repo_data = data.get('data', {}).get('repository', {})

                if not repo_data:
                    continue

                # Check each PR for user's review (not approved)
                pr_map = {pr.number: pr for pr in repo_prs}
                for i, pr_num in enumerate(pr_numbers[:100]):
                    pr_data = repo_data.get(f'pr{i}')
                    if not pr_data:
                        continue

                    reviews = pr_data.get('reviews', {}).get('nodes', [])

                    # Track latest review state per user
                    latest_review_by_user = {}
                    for review in reviews:
                        if not review.get('author'):
                            continue
                        user = review['author']['login']
                        state = review.get('state', '')
                        submitted_at = review.get('submittedAt')

                        if state in ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED'):
                            if user not in latest_review_by_user:
                                latest_review_by_user[user] = (state, submitted_at)
                            elif submitted_at > latest_review_by_user[user][1]:
                                old_state = latest_review_by_user[user][0]
                                if state in ('APPROVED', 'CHANGES_REQUESTED') or old_state == 'COMMENTED':
                                    latest_review_by_user[user] = (state, submitted_at)

                    # Check if user has reviewed but NOT approved (i.e., COMMENTED or CHANGES_REQUESTED)
                    if username in latest_review_by_user:
                        user_state = latest_review_by_user[username][0]
                        if user_state in ('COMMENTED', 'CHANGES_REQUESTED'):
                            if pr_num in pr_map:
                                reviewed_prs.append(pr_map[pr_num])

            except Exception as e:
                print(f"ERROR: Failed to filter reviewed PRs for {owner}/{name}: {e}")

        return reviewed_prs

    def _issue_to_pr_info_fast(self, issue, repo_owner: str, repo_name: str) -> PullRequestInfo:
        """Convert a PyGithub Issue (from search) to PullRequestInfo - FAST path with no extra API calls."""
        labels = [
            {
                'name': label.name,
                'color': label.color,
            }
            for label in issue.labels
        ]

        # Extract CI status from issue state if available
        # GitHub's search API includes some status info in the issue object
        ci_state = 'unknown'

        # Check if issue has pull_request attribute with status info
        if hasattr(issue, 'pull_request') and issue.pull_request:
            pr_data = issue.pull_request
            # Some status might be available in raw_data
            if hasattr(issue, '_rawData'):
                raw = issue._rawData
                if 'state_reason' in raw:
                    # Try to infer from state_reason
                    pass

        return PullRequestInfo(
            number=issue.number,
            title=issue.title,
            url=issue.html_url,
            repo_owner=repo_owner,
            repo_name=repo_name,
            author=issue.user.login,
            author_avatar=issue.user.avatar_url,
            created_at=issue.created_at,
            updated_at=issue.updated_at,
            labels=labels,
            ci_status=CIStatus(state=ci_state, passed_count=0, total_count=0),
            review_status=ReviewStatus(state='not_reviewed', approval_count=0, comment_count=0),
            draft='draft' in issue.title.lower(),  # Infer from title
            additions=0,  # Not available without extra API call
            deletions=0,  # Not available without extra API call
        )

    def _issue_to_pr_info(self, issue, repo_owner: str, repo_name: str) -> PullRequestInfo:
        """Convert a PyGithub Issue (from search) to PullRequestInfo without extra API calls."""
        labels = [
            {
                'name': label.name,
                'color': label.color,
            }
            for label in issue.labels
        ]

        # Get PR-specific data only if needed
        # Issue objects from search have most data we need
        # We'll skip CI status and draft flag to avoid extra API calls
        return PullRequestInfo(
            number=issue.number,
            title=issue.title,
            url=issue.html_url,
            repo_owner=repo_owner,
            repo_name=repo_name,
            author=issue.user.login,
            author_avatar=issue.user.avatar_url,
            created_at=issue.created_at,
            updated_at=issue.updated_at,
            labels=labels,
            ci_status=CIStatus(state='unknown'),  # Skip CI check for performance
            review_status=ReviewStatus(state='not_reviewed', approval_count=0, comment_count=0),
            draft=False,  # Can't get this from Issue without extra API call
            additions=0,  # Can't get this from Issue without extra API call
            deletions=0,  # Can't get this from Issue without extra API call
        )

    def get_reviews_for_stats(self, repos: list[tuple[str, str]], username: str, days: int = 30) -> dict:
        """Get review statistics for a user across repos.

        Returns dict with:
        - reviews_given: count of reviews the user has given
        - reviews_received: count of reviews received on user's PRs
        - avg_turnaround_hours: average time to first review
        - top_reviewers: list of {username, avatar_url, count} for PRs user reviewed
        - top_reviewed_by: list of {username, avatar_url, count} for who reviews user's PRs
        """
        if not repos or not username:
            return {
                'reviews_given': 0,
                'reviews_received': 0,
                'avg_turnaround_hours': 0.0,
                'top_reviewers': [],
                'top_reviewed_by': [],
            }

        token = self._get_token()
        if not token:
            return {
                'reviews_given': 0,
                'reviews_received': 0,
                'avg_turnaround_hours': 0.0,
                'top_reviewers': [],
                'top_reviewed_by': [],
            }

        import requests
        from datetime import datetime, timedelta
        from collections import defaultdict

        cutoff = datetime.now() - timedelta(days=days)
        cutoff_iso = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')

        reviews_given_to = defaultdict(lambda: {'count': 0, 'avatar': ''})  # username -> {count, avatar}
        reviews_received_from = defaultdict(lambda: {'count': 0, 'avatar': ''})  # username -> {count, avatar}
        reviews_given_count = 0
        reviews_received_count = 0
        total_turnaround = 0.0
        turnaround_count = 0

        for owner, name in repos:
            # Query: PRs where user is author (to get reviews received)
            query_received = f'''
                query {{
                    search(query: "repo:{owner}/{name} is:pr author:{username} updated:>{cutoff_iso[:10]}", type: ISSUE, first: 50) {{
                        nodes {{
                            ... on PullRequest {{
                                number
                                createdAt
                                reviews(first: 100) {{
                                    nodes {{
                                        author {{
                                            login
                                            avatarUrl
                                        }}
                                        state
                                        submittedAt
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}
            '''

            # Query: PRs where user reviewed (to get reviews given)
            query_given = f'''
                query {{
                    search(query: "repo:{owner}/{name} is:pr reviewed-by:{username} updated:>{cutoff_iso[:10]}", type: ISSUE, first: 50) {{
                        nodes {{
                            ... on PullRequest {{
                                number
                                author {{
                                    login
                                    avatarUrl
                                }}
                                reviews(first: 100) {{
                                    nodes {{
                                        author {{
                                            login
                                        }}
                                        state
                                        submittedAt
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}
            '''

            try:
                # Fetch PRs where user is author
                resp_received = requests.post(
                    'https://api.github.com/graphql',
                    json={'query': query_received},
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Content-Type': 'application/json',
                    },
                    timeout=30
                )

                if resp_received.status_code == 200:
                    data = resp_received.json()
                    prs = data.get('data', {}).get('search', {}).get('nodes', [])

                    for pr in prs:
                        if not pr:
                            continue
                        reviews = pr.get('reviews', {}).get('nodes', [])
                        pr_created = pr.get('createdAt')

                        seen_reviewers = set()
                        first_review_time = None

                        for review in reviews:
                            if not review or not review.get('author'):
                                continue
                            reviewer = review['author']['login']
                            avatar = review['author'].get('avatarUrl', '')
                            submitted = review.get('submittedAt')

                            if reviewer == username:
                                continue  # Skip self-reviews

                            if reviewer not in seen_reviewers:
                                seen_reviewers.add(reviewer)
                                reviews_received_from[reviewer]['count'] += 1
                                if avatar:
                                    reviews_received_from[reviewer]['avatar'] = avatar

                            # Track first review time
                            if submitted and (first_review_time is None or submitted < first_review_time):
                                first_review_time = submitted

                        reviews_received_count += len(seen_reviewers)

                        # Calculate turnaround
                        if pr_created and first_review_time:
                            try:
                                created_dt = datetime.fromisoformat(pr_created.replace('Z', '+00:00'))
                                review_dt = datetime.fromisoformat(first_review_time.replace('Z', '+00:00'))
                                turnaround_hours = (review_dt - created_dt).total_seconds() / 3600
                                if turnaround_hours > 0:
                                    total_turnaround += turnaround_hours
                                    turnaround_count += 1
                            except Exception:
                                pass

                # Fetch PRs where user reviewed
                resp_given = requests.post(
                    'https://api.github.com/graphql',
                    json={'query': query_given},
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Content-Type': 'application/json',
                    },
                    timeout=30
                )

                if resp_given.status_code == 200:
                    data = resp_given.json()
                    prs = data.get('data', {}).get('search', {}).get('nodes', [])

                    for pr in prs:
                        if not pr:
                            continue
                        author_data = pr.get('author')
                        if not author_data:
                            continue
                        pr_author = author_data['login']
                        pr_avatar = author_data.get('avatarUrl', '')

                        if pr_author == username:
                            continue  # Skip own PRs

                        reviews = pr.get('reviews', {}).get('nodes', [])
                        user_reviewed = False

                        for review in reviews:
                            if not review or not review.get('author'):
                                continue
                            reviewer = review['author']['login']
                            if reviewer == username:
                                user_reviewed = True
                                break

                        if user_reviewed:
                            reviews_given_count += 1
                            reviews_given_to[pr_author]['count'] += 1
                            if pr_avatar:
                                reviews_given_to[pr_author]['avatar'] = pr_avatar

            except Exception as e:
                print(f"ERROR: Failed to fetch review stats for {owner}/{name}: {e}")

        # Build top reviewers (authors of PRs that user reviewed)
        top_reviewers = [
            {'username': author, 'count': data['count'], 'avatar_url': data['avatar']}
            for author, data in sorted(reviews_given_to.items(), key=lambda x: -x[1]['count'])[:10]
        ]

        # Build top reviewed by
        top_reviewed_by = [
            {'username': reviewer, 'count': data['count'], 'avatar_url': data['avatar']}
            for reviewer, data in sorted(reviews_received_from.items(), key=lambda x: -x[1]['count'])[:10]
        ]

        avg_turnaround = total_turnaround / turnaround_count if turnaround_count > 0 else 0.0

        return {
            'reviews_given': reviews_given_count,
            'reviews_received': reviews_received_count,
            'avg_turnaround_hours': avg_turnaround,
            'top_reviewers': top_reviewers,
            'top_reviewed_by': top_reviewed_by,
        }

    def _pr_to_info(self, pr, repo_owner: str, repo_name: str) -> PullRequestInfo:
        """Convert a PyGithub PullRequest to PullRequestInfo."""
        labels = [
            {
                'name': label.name,
                'color': label.color,
            }
            for label in pr.labels
        ]

        mergeable = None
        cache_key = f"pr_mergeable:{repo_owner}/{repo_name}:{pr.number}"
        if pr.mergeable is True:
            mergeable = 'MERGEABLE'
            cache.set(cache_key, mergeable, 3600)
        elif pr.mergeable is False:
            mergeable = 'CONFLICTING'
            cache.set(cache_key, mergeable, 3600)
        else:
            mergeable = cache.get(cache_key)

        # Get head repository (could be a fork)
        head_repo_owner = pr.head.repo.owner.login if pr.head.repo else repo_owner
        head_repo_name = pr.head.repo.name if pr.head.repo else repo_name

        return PullRequestInfo(
            number=pr.number,
            title=pr.title,
            url=pr.html_url,
            repo_owner=repo_owner,
            repo_name=repo_name,
            author=pr.user.login,
            author_avatar=pr.user.avatar_url,
            created_at=pr.created_at,
            updated_at=pr.updated_at,
            labels=labels,
            ci_status=self._get_ci_status(pr),
            review_status=self._get_review_status(pr),
            draft=pr.draft,
            additions=pr.additions,
            deletions=pr.deletions,
            branch_name=pr.head.ref,
            head_repo_owner=head_repo_owner,
            head_repo_name=head_repo_name,
            mergeable=mergeable,
        )
