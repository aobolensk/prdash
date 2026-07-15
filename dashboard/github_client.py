"""GitHub API client for fetching PR information."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
from allauth.socialaccount.models import SocialAccount
from django.core.cache import cache
from github import RateLimitExceededException
from urllib3.exceptions import MaxRetryError


logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = 'https://api.github.com/graphql'
GITHUB_API_VERSION = '2022-11-28'
GITHUB_PROVIDER = 'github'
USERNAME_CACHE_TTL_SECONDS = 86400
GRAPHQL_PR_BATCH_SIZE = 25
GRAPHQL_RETRY_ATTEMPTS = 2
GRAPHQL_RETRY_BACKOFF_SECONDS = 0.5
GRAPHQL_TIMEOUT_SECONDS = 20
GRAPHQL_TRANSIENT_STATUS_CODES = {502, 503, 504}


@dataclass(slots=True)
class CIStatus:
    """Represents the CI status of a pull request."""
    state: str  # 'success', 'pending', 'failure', 'error', or 'unknown'
    passed_count: int = 0
    total_count: int = 0
    context: Optional[str] = None
    description: Optional[str] = None
    target_url: Optional[str] = None


@dataclass(slots=True)
class ReviewStatus:
    """Represents the review status of a pull request."""
    state: str  # 'approved', 'changes_requested', 'not_reviewed'
    approval_count: int = 0
    comment_count: int = 0
    review_decision: Optional[str] = None  # 'APPROVED', 'CHANGES_REQUESTED', 'REVIEW_REQUIRED', or None


@dataclass(slots=True)
class LinkedIssue:
    """Represents a linked issue."""
    number: int
    url: str


@dataclass(slots=True)
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
    auto_merge_enabled: bool = False
    linked_issues: list[LinkedIssue] = None

    def __post_init__(self):
        if self.linked_issues is None:
            self.linked_issues = []

    @property
    def repo_full_name(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"


class GitHubClient:
    """Client for interacting with the GitHub API."""

    @staticmethod
    def _compute_latest_review_states(
        reviews: list[dict],
        author_key: str = 'author',
        login_key: str = 'login',
        state_key: str = 'state',
        time_key: str = 'submittedAt',
    ) -> dict[str, tuple[str, str]]:
        """Compute the latest review state per user from a list of reviews.

        Returns dict mapping username -> (state, submitted_at).
        Only considers APPROVED, CHANGES_REQUESTED, COMMENTED states.
        COMMENTED reviews don't override APPROVED/CHANGES_REQUESTED.
        """
        latest: dict[str, tuple[str, str]] = {}
        for review in reviews:
            author = review.get(author_key)
            if not author:
                continue
            user = author.get(login_key) if isinstance(author, dict) else author
            if not user:
                continue
            state = review.get(state_key, '')
            submitted_at = review.get(time_key)

            if state in ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED'):
                if user not in latest:
                    latest[user] = (state, submitted_at)
                elif submitted_at and submitted_at > latest[user][1]:
                    old_state = latest[user][0]
                    if state in ('APPROVED', 'CHANGES_REQUESTED') or old_state == 'COMMENTED':
                        latest[user] = (state, submitted_at)
        return latest

    def __init__(self, user):
        self.user = user
        self._client = None
        self._username = None  # Cached GitHub username
        self._grouped_errors = {}  # {(error_type, detail): [repo1, repo2, ...]}
        self._grouped_warnings = {}  # {(warning_type, detail): [repo1, repo2, ...]}
        self._rate_limited_repos = set()

    @property
    def client(self):
        """Lazily initialize the GitHub client with the user's OAuth token."""
        if self._client is None:
            token = self._get_token()
            if token:
                from github import Github
                # Disable automatic retries so we can handle rate limits ourselves
                self._client = Github(token, timeout=10, retry=0)
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
        except Exception:
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
        except Exception:
            return ReviewStatus(state='not_reviewed', approval_count=0, comment_count=0)

    def _add_grouped_message(
        self, collection: dict, msg_type: str, repo_name: str, detail: str
    ) -> None:
        """Add a message to a grouped collection."""
        key = (msg_type, detail)
        if key not in collection:
            collection[key] = []
        if repo_name and repo_name not in collection[key]:
            collection[key].append(repo_name)

    def _add_error(self, error_type: str, repo_name: str = '', detail: str = '') -> None:
        """Add a structured error, grouped by type and detail."""
        self._add_grouped_message(self._grouped_errors, error_type, repo_name, detail)

    def _add_warning(self, warning_type: str, repo_name: str = '', detail: str = '') -> None:
        """Add a structured warning, grouped by type and detail."""
        self._add_grouped_message(self._grouped_warnings, warning_type, repo_name, detail)

    @staticmethod
    def _format_grouped_messages(grouped: dict) -> list[str]:
        """Format grouped messages into a list of strings."""
        result = []
        for (msg_type, detail), repos in grouped.items():
            msg = f"{msg_type}: {', '.join(repos)}" if repos else msg_type
            if detail:
                msg += f": {detail}"
            result.append(msg)
        return result

    @property
    def errors(self) -> list[str]:
        """Return formatted error messages grouped by type."""
        return self._format_grouped_messages(self._grouped_errors)

    @property
    def warnings(self) -> list[str]:
        """Return formatted warning messages grouped by type."""
        return self._format_grouped_messages(self._grouped_warnings)

    @staticmethod
    def _repo_label(owner: Optional[str], name: Optional[str]) -> str:
        """Return a display name for a repo-scoped GitHub request."""
        return f"{owner}/{name}" if owner and name else "GitHub"

    @staticmethod
    def _iter_chunks(items: list, size: int):
        """Yield fixed-size chunks from a list."""
        for start in range(0, len(items), size):
            yield items[start:start + size]

    def _summarize_response(self, response: requests.Response) -> str:
        """Return a short, safe description of a GitHub error response."""
        try:
            data = response.json()
        except ValueError:
            text = (response.text or '').strip()
            headers = getattr(response, 'headers', {}) or {}
            content_type = headers.get('Content-Type', '')
            sample = text[:500].lower()

            if (
                'html' in content_type.lower()
                or '<html' in sample
                or '<!doctype html' in sample
            ):
                return 'HTML error page omitted'
            if not text:
                return 'empty response body'
            return ' '.join(text.split())[:200]

        if isinstance(data, dict):
            message = data.get('message') or data.get('error')
            if message:
                return str(message)[:200]

            errors = data.get('errors')
            if isinstance(errors, list) and errors:
                first_error = errors[0]
                if isinstance(first_error, dict):
                    message = first_error.get('message')
                    if message:
                        return str(message)[:200]
                return str(first_error)[:200]

        return 'JSON response without error message'

    def _handle_error(
        self,
        owner: str,
        name: str,
        status_code: Optional[int] = None,
        message: str = '',
        error_type: Optional[str] = None,
        operation: str = 'GitHub request',
    ) -> None:
        """Unified error handler for all GitHub API errors.

        Args:
            owner: Repository owner
            name: Repository name
            status_code: HTTP status code (if applicable)
            message: Error message from API
            error_type: GraphQL error type (if applicable)
            operation: Description of the operation that failed
        """
        repo_name = self._repo_label(owner, name)
        message_lower = message.lower()

        # Log the error
        if status_code:
            logger.warning("%s failed for %s: HTTP %s; %s", operation, repo_name, status_code, message)
        else:
            logger.warning("%s failed for %s: %s %s", operation, repo_name, error_type or 'Error', message)

        # Check for rate limiting
        if (
            (status_code == 403 and 'rate limit' in message_lower)
            or error_type == 'RATE_LIMITED'
            or 'rate limit' in message_lower
        ):
            self._rate_limited_repos.add(repo_name)
            return

        # Check for SAML enforcement (case-insensitive)
        if 'saml' in message_lower:
            self._add_error("Access denied", repo_name, "SAML SSO required")
            return

        # Handle specific status codes and error types
        if status_code == 401:
            self._add_error("GitHub authentication failed", detail="Reconnect GitHub or update your PAT")
        elif status_code == 403 or error_type == 'FORBIDDEN':
            detail = message if message and not message_lower.startswith('github') else ''
            self._add_error("Access denied", repo_name, detail)
        elif status_code == 404 or error_type == 'NOT_FOUND':
            self._add_error("Repository not found or not accessible", repo_name)
        elif status_code in GRAPHQL_TRANSIENT_STATUS_CODES:
            self._add_warning(
                "GitHub temporarily unavailable. Try refreshing",
                repo_name,
                f"HTTP {status_code}",
            )
        elif 'timeout' in message_lower or 'timed out' in message_lower:
            self._add_warning("GitHub timed out. Try refreshing", repo_name)
        elif status_code:
            self._add_warning(f"GitHub returned HTTP {status_code}", repo_name)
        else:
            self._add_error("Failed to fetch PRs", repo_name)

    def _post_graphql(
        self,
        query: str,
        owner: Optional[str] = None,
        name: Optional[str] = None,
        operation: str = 'GitHub GraphQL query',
        token: Optional[str] = None,
        max_attempts: int = GRAPHQL_RETRY_ATTEMPTS,
    ) -> Optional[dict]:
        """Execute a GitHub GraphQL query with retries and safe error reporting."""
        token = token or self._get_token()
        if not token:
            return None

        repo_name = self._repo_label(owner, name)
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json',
            'X-GitHub-Api-Version': GITHUB_API_VERSION,
        }

        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(
                    GITHUB_GRAPHQL_URL,
                    json={'query': query},
                    headers=headers,
                    timeout=GRAPHQL_TIMEOUT_SECONDS,
                )
            except requests.exceptions.Timeout:
                if attempt < max_attempts:
                    time.sleep(GRAPHQL_RETRY_BACKOFF_SECONDS * attempt)
                    continue

                logger.warning("%s timed out for %s", operation, repo_name)
                self._add_warning("GitHub timed out. Try refreshing", repo_name)
                return None
            except requests.exceptions.RequestException:
                logger.warning("%s request failed for %s", operation, repo_name, exc_info=True)
                self._add_warning("GitHub request failed. Try refreshing", repo_name)
                return None

            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    summary = self._summarize_response(response)
                    logger.warning(
                        "%s returned non-JSON response for %s: %s",
                        operation,
                        repo_name,
                        summary,
                    )
                    self._add_warning("GitHub returned invalid response. Try refreshing", repo_name)
                    return None

                # Handle GraphQL errors from 200 response
                errors = data.get('errors')
                if errors:
                    for error in errors:
                        if not isinstance(error, dict):
                            logger.warning("%s returned GraphQL error for %s: %s", operation, repo_name, error)
                            continue

                        error_type = error.get('type') or error.get('extensions', {}).get('code')
                        message = error.get('message', '')
                        self._handle_error(
                            owner or '',
                            name or '',
                            error_type=error_type,
                            message=message,
                            operation=operation,
                        )

                return data

            if response.status_code in GRAPHQL_TRANSIENT_STATUS_CODES and attempt < max_attempts:
                summary = self._summarize_response(response)
                logger.warning(
                    "%s returned transient HTTP %s for %s; retrying. %s",
                    operation,
                    response.status_code,
                    repo_name,
                    summary,
                )
                time.sleep(GRAPHQL_RETRY_BACKOFF_SECONDS * attempt)
                continue

            # Handle non-200 HTTP response
            summary = self._summarize_response(response)
            self._handle_error(
                owner or '',
                name or '',
                status_code=response.status_code,
                message=summary,
                operation=operation,
            )
            return None

        return None

    def get_notification_triggers(self) -> dict:
        """Get HX-Trigger dict for errors and warnings."""
        triggers = {}
        if self.errors:
            triggers['showErrors'] = self.errors
        if self.warnings:
            triggers['showWarnings'] = self.warnings
        return triggers

    def _is_rate_limit_error(self, e: Exception) -> bool:
        """Check if exception is a rate limit error."""
        if isinstance(e, RateLimitExceededException):
            return True
        if isinstance(e, MaxRetryError):
            return True
        if not (hasattr(e, 'status') and e.status == 403):
            return False
        if not (hasattr(e, 'data') and isinstance(e.data, dict)):
            return True
        api_msg = e.data.get('message', '').lower()
        return 'rate limit' in api_msg

    def _handle_api_error(self, e: Exception, owner: str, name: str) -> None:
        """Handle GitHub API errors from PyGithub exceptions."""
        if self._is_rate_limit_error(e):
            self._rate_limited_repos.add(f"{owner}/{name}")
            return

        status_code = e.status if hasattr(e, 'status') else None
        message = e.data.get('message', '') if hasattr(e, 'data') and isinstance(e.data, dict) else str(e)

        self._handle_error(owner, name, status_code=status_code, message=message)

    def validate_repo(self, owner: str, name: str) -> tuple[bool, str]:
        """Validate that a repository exists and is accessible."""
        if not self.client:
            return False, "Not authenticated with GitHub"

        try:
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
                    if detailed_msg:
                        error_msg = f"Error accessing {owner}/{name}: {detailed_msg}"
                    else:
                        error_msg = f"Error accessing {owner}/{name}"
                return False, error_msg
            return False, f"Error accessing {owner}/{name}: {str(e)}"

    def _fetch_prs_batch_graphql(self, owner: str, name: str, pr_numbers: list[int]) -> list[PullRequestInfo]:
        """Fetch multiple PRs using GraphQL to minimize API calls."""
        if not pr_numbers:
            return []

        if len(pr_numbers) > GRAPHQL_PR_BATCH_SIZE:
            result = []
            for pr_number_batch in self._iter_chunks(pr_numbers, GRAPHQL_PR_BATCH_SIZE):
                result.extend(self._fetch_prs_batch_graphql(owner, name, pr_number_batch))
            return result

        try:
            # Build GraphQL query to fetch all PR data at once
            # Keep batches bounded so GitHub does not time out on large repos.
            pr_queries = []
            for i, pr_num in enumerate(pr_numbers):
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
                        autoMergeRequest {{
                            enabledAt
                        }}
                        headRefName
                        headRepository {{
                            owner {{
                                login
                            }}
                            name
                        }}
                        closingIssuesReferences(first: 10) {{
                            nodes {{
                                number
                                url
                            }}
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
                                                    checkSuite {{
                                                        workflowRun {{
                                                            runNumber
                                                            workflow {{
                                                                name
                                                            }}
                                                        }}
                                                    }}
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
                        reviewDecision
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
            data = self._post_graphql(
                query,
                owner=owner,
                name=name,
                operation='PR details GraphQL query',
            )
            if data is None:
                return []

            # Parse results
            result = []
            repo_data = data.get('data', {}).get('repository', {})

            if not repo_data:
                return []

            for i, pr_num in enumerate(pr_numbers):
                pr_data = repo_data.get(f'pr{i}')
                if not pr_data:
                    continue

                pr_info = self._parse_pr_from_graphql(pr_data, owner, name)
                if pr_info:
                    result.append(pr_info)

            return result

        except Exception:
            return []

    def _fetch_prs_multi_repo_graphql(
        self, pr_data: dict[tuple[str, str], list[int]]
    ) -> list[PullRequestInfo]:
        """Fetch PRs from multiple repos in a single GraphQL query.

        This reduces API calls from N (one per repo) to ceil(total_prs / batch_size).
        """
        if not pr_data:
            return []

        # Flatten to list of (owner, name, pr_number) for batching
        all_prs: list[tuple[str, str, int]] = []
        for (owner, name), pr_numbers in pr_data.items():
            for pr_num in pr_numbers:
                all_prs.append((owner, name, pr_num))

        if not all_prs:
            return []

        result = []
        batch_size = 50

        for batch in self._iter_chunks(all_prs, batch_size):
            batch_result = self._fetch_pr_batch_multi_repo(batch)
            result.extend(batch_result)

        return result

    def _fetch_pr_batch_multi_repo(
        self, prs: list[tuple[str, str, int]]
    ) -> list[PullRequestInfo]:
        """Fetch a batch of PRs from multiple repos in one GraphQL query."""
        if not prs:
            return []

        # Build query with aliased repository blocks
        pr_queries = []
        alias_map: dict[str, tuple[str, str]] = {}

        for i, (owner, name, pr_num) in enumerate(prs):
            alias = f"r{i}"
            alias_map[alias] = (owner, name)
            pr_queries.append(f'''
                {alias}: repository(owner: "{owner}", name: "{name}") {{
                    pullRequest(number: {pr_num}) {{
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
                        autoMergeRequest {{
                            enabledAt
                        }}
                        headRefName
                        headRepository {{
                            owner {{
                                login
                            }}
                            name
                        }}
                        closingIssuesReferences(first: 10) {{
                            nodes {{
                                number
                                url
                            }}
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
                                                    checkSuite {{
                                                        workflowRun {{
                                                            runNumber
                                                            workflow {{
                                                                name
                                                            }}
                                                        }}
                                                    }}
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
                        reviewDecision
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
                }}
            ''')

        query = f'''
            query {{
                {' '.join(pr_queries)}
            }}
        '''

        data = self._post_graphql(
            query,
            operation='Multi-repo PR details GraphQL query',
        )
        if data is None:
            return []

        result = []
        query_data = data.get('data', {})

        for alias, (owner, name) in alias_map.items():
            repo_data = query_data.get(alias)
            if not repo_data:
                continue

            pr_node = repo_data.get('pullRequest')
            if not pr_node:
                continue

            pr_info = self._parse_pr_from_graphql(pr_node, owner, name)
            if pr_info:
                result.append(pr_info)

        return result

    def _parse_pr_from_graphql(
        self, pr_data: dict, owner: str, name: str
    ) -> Optional[PullRequestInfo]:
        """Parse a single PR from GraphQL response into PullRequestInfo."""
        try:
            if not pr_data or not pr_data.get('author'):
                return None

            labels = [
                {'name': label['name'], 'color': label['color']}
                for label in pr_data.get('labels', {}).get('nodes', [])
            ]

            ci_status = self._parse_ci_status_from_graphql(pr_data)
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

            head_repo = pr_data.get('headRepository')
            head_repo_owner = owner
            head_repo_name = name
            if head_repo and head_repo.get('owner'):
                head_repo_owner = head_repo['owner']['login']
                head_repo_name = head_repo['name']

            auto_merge_request = pr_data.get('autoMergeRequest')
            auto_merge_enabled = auto_merge_request is not None and auto_merge_request.get('enabledAt') is not None

            linked_issues = [
                LinkedIssue(number=issue['number'], url=issue['url'])
                for issue in pr_data.get('closingIssuesReferences', {}).get('nodes', [])
                if issue
            ]

            return PullRequestInfo(
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
                auto_merge_enabled=auto_merge_enabled,
                linked_issues=linked_issues,
            )
        except Exception:
            return None

    @staticmethod
    def _drop_superseded_check_runs(contexts: list[dict]) -> list[dict]:
        """Remove CheckRuns from workflow runs that have been superseded by a re-run.

        When a workflow is re-run, GitHub keeps the CheckRuns from the old run
        attached to the commit's statusCheckRollup, but its PR UI only shows the
        latest run per workflow. Without this filtering a green PR whose earlier
        run failed/was cancelled would incorrectly report a failing CI status.

        Keeps StatusContexts untouched and CheckRuns lacking workflow metadata.
        """
        def workflow_key(ctx: dict) -> tuple[Optional[str], Optional[int]]:
            workflow = (ctx.get('checkSuite') or {}).get('workflowRun') or {}
            return (workflow.get('workflow') or {}).get('name'), workflow.get('runNumber')

        latest_run: dict[str, int] = {}
        for ctx in contexts:
            if 'conclusion' not in ctx:
                continue
            name, run_number = workflow_key(ctx)
            if name is None or run_number is None:
                continue
            if name not in latest_run or run_number > latest_run[name]:
                latest_run[name] = run_number

        if not latest_run:
            return contexts

        kept = []
        for ctx in contexts:
            if 'conclusion' not in ctx:
                kept.append(ctx)
                continue
            name, run_number = workflow_key(ctx)
            if name is not None and run_number is not None and run_number != latest_run[name]:
                continue
            kept.append(ctx)
        return kept

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
            raw_nodes = contexts_data.get('nodes', [])
            # If GitHub truncated the contexts page, a failing check may be
            # off-page; we can't recompute reliably, so trust rollup.state.
            truncated = contexts_data.get('totalCount', 0) > len(raw_nodes)
            contexts = self._drop_superseded_check_runs(raw_nodes)
            total_count = len(contexts)
            rollup_state = {
                'ERROR': 'error',
                'EXPECTED': 'pending',
                'FAILURE': 'failure',
                'PENDING': 'pending',
                'SUCCESS': 'success',
            }.get(rollup.get('state'))

            if total_count == 0:
                return CIStatus(state=rollup_state or 'unknown')

            if truncated and rollup_state:
                return CIStatus(
                    state=rollup_state,
                    passed_count=sum(
                        1 for c in contexts
                        if c.get('conclusion') == 'SUCCESS' or c.get('state') == 'SUCCESS'
                    ),
                    total_count=contexts_data.get('totalCount', total_count),
                )

            success_count = 0
            failure_count = 0
            skipped_count = 0
            pending_count = 0

            for context in contexts:
                # Check if it's a CheckRun or StatusContext
                if 'conclusion' in context:  # CheckRun
                    conclusion = context.get('conclusion')

                    if conclusion == 'SUCCESS':
                        success_count += 1
                    elif conclusion in (
                        'FAILURE', 'CANCELLED', 'TIMED_OUT',
                        'ACTION_REQUIRED', 'STARTUP_FAILURE',
                    ):
                        failure_count += 1
                    elif conclusion in ('SKIPPED', 'NEUTRAL', 'STALE'):
                        skipped_count += 1
                    elif conclusion is None:
                        # No conclusion yet: the check is still queued/running.
                        pending_count += 1

                elif 'state' in context:  # StatusContext
                    state_value = context.get('state')
                    if state_value == 'SUCCESS':
                        success_count += 1
                    elif state_value in ('FAILURE', 'ERROR'):
                        failure_count += 1
                    elif state_value in ('PENDING', 'EXPECTED'):
                        pending_count += 1

            # Derive state from the deduped checks rather than trusting
            # rollup.state: the rollup aggregates every workflow run, including
            # superseded re-runs, so a green PR whose earlier run failed still
            # reports FAILURE. GitHub's own PR UI only considers the latest run
            # per workflow, which is what the dedup above reproduces.
            if failure_count > 0:
                state = 'failure'
            elif pending_count > 0:
                state = 'pending'
            elif success_count > 0:
                state = 'success'
            elif skipped_count == total_count:
                state = 'success'
            else:
                state = rollup_state or 'unknown'

            return CIStatus(
                state=state,
                passed_count=success_count,
                total_count=total_count
            )
        except Exception:
            return CIStatus(state='unknown')

    def _parse_review_status_from_graphql(self, pr_data: dict) -> ReviewStatus:
        """Parse review status from GraphQL response."""
        try:
            reviews = pr_data.get('reviews', {}).get('nodes', [])
            comment_count = pr_data.get('comments', {}).get('totalCount', 0)
            comment_count += pr_data.get('reviewThreads', {}).get('totalCount', 0)
            review_decision = pr_data.get('reviewDecision')

            latest_review_by_user = self._compute_latest_review_states(reviews)

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
                comment_count=comment_count,
                review_decision=review_decision,
            )
        except Exception:
            return ReviewStatus(state='not_reviewed', approval_count=0, comment_count=0)

    def get_username(self) -> Optional[str]:
        """Get the authenticated user's GitHub username (cached).

        Uses multiple layers of caching: instance -> Django cache -> SocialAccount -> API.
        """
        if self._username:
            return self._username

        cache_key = f"github_username:{self.user.id}"
        cached_username = cache.get(cache_key)
        if cached_username:
            self._username = cached_username
            return self._username

        login = self._get_username_from_social_account()
        if login:
            self._username = login
            cache.set(cache_key, login, USERNAME_CACHE_TTL_SECONDS)
            return self._username

        login = self._get_username_from_api()
        if login:
            self._username = login
            cache.set(cache_key, login, USERNAME_CACHE_TTL_SECONDS)
        return login

    def _get_username_from_social_account(self) -> Optional[str]:
        """Get username from SocialAccount.extra_data (stored at OAuth login)."""
        try:
            extra_data = SocialAccount.objects.filter(
                user=self.user,
                provider=GITHUB_PROVIDER
            ).only('extra_data').values_list('extra_data', flat=True).first()
            if extra_data:
                return extra_data.get('login')
        except Exception as e:
            logger.debug("Failed to get username from SocialAccount: %s", e)
        return None

    def _get_username_from_api(self) -> Optional[str]:
        """Get username from PyGithub API (fallback, can be sporadic)."""
        if not self.client:
            return None
        try:
            return self.client.get_user().login
        except Exception:
            return None

    def finalize_warnings(self) -> None:
        """Convert rate-limited repos into a single consolidated warning message."""
        if not self._rate_limited_repos:
            return

        count = len(self._rate_limited_repos)
        if count <= 3:
            repos_str = ', '.join(sorted(self._rate_limited_repos))
            self._add_warning(f"Rate limit hit. Not loaded: {repos_str}")
        else:
            self._add_warning(f"Rate limit hit. {count} repos not loaded.")

    def _search_prs(
        self, query: str, owner: str, name: str, limit: Optional[int] = None
    ) -> list[int]:
        """Search for PR numbers using REST API, paginating up to GitHub's 1000-result cap."""
        token = self._get_token()
        if not token:
            return []

        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }

        seen: set[int] = set()
        pr_numbers: list[int] = []
        page = 1

        try:
            while True:
                response = requests.get(
                    'https://api.github.com/search/issues',
                    params={
                        'q': query,
                        'sort': 'updated',
                        'order': 'desc',
                        'per_page': 100,
                        'page': page,
                    },
                    headers=headers,
                    timeout=30,
                )

                if response.status_code == 403:
                    if 'rate limit' in response.text.lower():
                        self._rate_limited_repos.add(f"{owner}/{name}")
                    else:
                        self._handle_api_error_from_response(response, owner, name)
                    break

                if response.status_code != 200:
                    self._handle_api_error_from_response(response, owner, name)
                    break

                data = response.json()
                items = data.get('items', [])
                for item in items:
                    n = item['number']
                    if n not in seen:
                        seen.add(n)
                        pr_numbers.append(n)

                if limit and len(pr_numbers) >= limit:
                    return pr_numbers[:limit]

                # GitHub Search API caps at 1000 results; stop if this page wasn't full
                if len(items) < 100:
                    break

                page += 1

        except requests.exceptions.RequestException:
            pass

        return pr_numbers

    def _handle_api_error_from_response(
        self, response: requests.Response, owner: str, name: str
    ) -> None:
        """Handle errors from requests.Response objects."""
        message = self._summarize_response(response)
        self._handle_error(owner, name, status_code=response.status_code, message=message)

    def get_user_prs_for_repo(self, owner: str, name: str, author: Optional[str] = None) -> list[PullRequestInfo]:
        """Get open PRs authored by the specified user (or current user) for a specific repository."""
        if not self.client:
            return []

        try:
            # Use provided author or default to authenticated user
            if author is None:
                author = self.get_username()
                if not author:
                    return []

            # Use GitHub Search API
            query = f"repo:{owner}/{name} is:pr is:open author:{author}"
            pr_numbers = self._search_prs(query, owner, name)

            if not pr_numbers:
                return []

            result = self._fetch_prs_batch_graphql(owner, name, pr_numbers)

            result.sort(key=lambda pr: (pr.updated_at, pr.number), reverse=True)
            return result
        except Exception as e:
            self._handle_api_error(e, owner, name)
            return []

    def get_all_user_prs(self, repos: list[tuple[str, str]], author: Optional[str] = None) -> list[PullRequestInfo]:
        """Get all open PRs authored by the specified user (or current user) across multiple repositories.

        Uses a single consolidated search query instead of one per repo to reduce API calls.
        """
        if not repos:
            return []

        if author is None:
            author = self.get_username()
            if not author:
                return []

        # Single search for ALL open PRs by this author (1 API call instead of N)
        query = f"is:pr is:open author:{author}"
        pr_data = self._search_prs_consolidated(query, repos)

        if not pr_data:
            self.finalize_warnings()
            return []

        # Fetch all PRs across repos in batched multi-repo GraphQL queries
        all_prs = self._fetch_prs_multi_repo_graphql(pr_data)

        self.finalize_warnings()
        all_prs.sort(key=lambda pr: (pr.updated_at, pr.number), reverse=True)
        return all_prs

    def _search_prs_consolidated(
        self, query: str, repos: list[tuple[str, str]]
    ) -> dict[tuple[str, str], list[int]]:
        """Search for PRs across all repos with a single API call, filter by tracked repos.

        Returns dict mapping (owner, name) -> [pr_numbers] for matching repos.
        """
        token = self._get_token()
        if not token:
            return {}

        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }

        try:
            response = requests.get(
                'https://api.github.com/search/issues',
                params={
                    'q': query,
                    'sort': 'updated',
                    'order': 'desc',
                    'per_page': 100,
                },
                headers=headers,
                timeout=30,
            )

            if response.status_code == 403:
                if 'rate limit' in response.text.lower():
                    self._add_warning("Rate limit hit. Some PRs may not be loaded.")
                return {}

            if response.status_code != 200:
                self._handle_error(
                    '', '', status_code=response.status_code,
                    message=self._summarize_response(response),
                    operation='PR search',
                )
                return {}

            data = response.json()
            items = data.get('items', [])

            tracked_repos = {(owner.lower(), name.lower()) for owner, name in repos}

            result: dict[tuple[str, str], list[int]] = {}
            for item in items:
                repo_url = item.get('repository_url', '')
                parts = repo_url.rstrip('/').split('/')
                if len(parts) >= 2:
                    owner, name = parts[-2], parts[-1]
                    if (owner.lower(), name.lower()) in tracked_repos:
                        key = (owner, name)
                        if key not in result:
                            result[key] = []
                        result[key].append(item['number'])

            return result

        except requests.exceptions.Timeout:
            self._add_warning("GitHub search timed out. Try refreshing.")
            return {}
        except requests.exceptions.RequestException:
            self._add_warning("GitHub search failed. Try refreshing.")
            return {}

    def get_merged_prs_for_repo(self, owner: str, name: str, author: Optional[str] = None) -> list[PullRequestInfo]:
        """Get recently merged PRs authored by the specified user (or current user) for a specific repository."""
        if not self.client:
            return []

        try:
            # Use provided author or default to authenticated user
            if author is None:
                author = self.get_username()
                if not author:
                    return []

            # Use GitHub Search API
            query = f"repo:{owner}/{name} is:pr is:merged author:{author}"
            pr_numbers = self._search_prs(query, owner, name)

            if not pr_numbers:
                return []

            result = self._fetch_prs_batch_graphql(owner, name, pr_numbers)

            result.sort(key=lambda pr: (pr.merged_at or pr.updated_at, pr.number), reverse=True)
            return result
        except Exception as e:
            self._handle_api_error(e, owner, name)
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
                    self._handle_api_error(e, owner, name)

        self.finalize_warnings()
        all_prs.sort(key=lambda pr: (pr.merged_at or pr.updated_at, pr.number), reverse=True)
        return all_prs

    def get_review_requests_for_repo(
        self, owner: str, name: str, approved_by_me: bool = False,
        reviewed_by_me: bool = False, include_all: bool = False,
        author: Optional[str] = None
    ) -> list[PullRequestInfo]:
        """Get open PRs where the current user's review is requested for a specific repository.

        Args:
            approved_by_me: If True, only return PRs that I have approved.
            reviewed_by_me: If True, return PRs that I have reviewed (any review state).
            include_all: If True, return all PRs where user is involved as reviewer
                (pending + reviewed + approved). Overrides other flags.
            author: If provided, filter PRs to only include those authored by this user.
        """
        if not self.client:
            return []

        try:
            username = self.get_username()
            if not username:
                return []

            if include_all:
                # Fetch both pending requests and reviewed PRs in parallel
                pending_query = f"repo:{owner}/{name} is:pr is:open review-requested:{username}"
                reviewed_query = f"repo:{owner}/{name} is:pr is:open reviewed-by:{username}"

                if author:
                    pending_query += f" author:{author}"
                    reviewed_query += f" author:{author}"

                # Run searches in parallel to reduce latency
                with ThreadPoolExecutor(max_workers=2) as executor:
                    pending_future = executor.submit(self._search_prs, pending_query, owner, name)
                    reviewed_future = executor.submit(self._search_prs, reviewed_query, owner, name)
                    pending_numbers = pending_future.result()
                    reviewed_numbers = reviewed_future.result()

                # Combine and deduplicate using set (order doesn't matter as results are sorted later)
                all_numbers = list(set(pending_numbers) | set(reviewed_numbers))

                if not all_numbers:
                    return []

                result = self._fetch_prs_batch_graphql(owner, name, all_numbers)
            else:
                if approved_by_me or reviewed_by_me:
                    # Search for PRs where I was a reviewer
                    query = f"repo:{owner}/{name} is:pr is:open reviewed-by:{username}"
                else:
                    # Search for PRs where review is requested from me
                    query = f"repo:{owner}/{name} is:pr is:open review-requested:{username}"

                # Add author filter if provided
                if author:
                    query += f" author:{author}"

                pr_numbers = self._search_prs(query, owner, name)

                if not pr_numbers:
                    return []

                result = self._fetch_prs_batch_graphql(owner, name, pr_numbers)

            result.sort(key=lambda pr: (pr.updated_at, pr.number), reverse=True)
            return result
        except Exception as e:
            self._handle_api_error(e, owner, name)
            return []

    def get_all_review_requests(
        self, repos: list[tuple[str, str]], approved_by_me: bool = False,
        reviewed_by_me: bool = False, include_all: bool = False,
        author: Optional[str] = None
    ) -> list[PullRequestInfo]:
        """Get all open PRs where the current user's review is requested across multiple repositories.

        Args:
            approved_by_me: If True, only return PRs that I have approved.
            reviewed_by_me: If True, return PRs that I have reviewed (any review state).
            include_all: If True, return all PRs where user is involved as reviewer
                (pending + reviewed + approved). Overrides other flags.
            author: If provided, filter PRs to only include those authored by this user.
        """
        if not repos:
            return []

        all_prs = []
        username = self.get_username()

        # Fetch PRs from all repos in parallel
        with ThreadPoolExecutor(max_workers=min(10, len(repos))) as executor:
            future_to_repo = {
                executor.submit(
                    self.get_review_requests_for_repo, owner, name,
                    approved_by_me, reviewed_by_me, include_all, author
                ): (owner, name)
                for owner, name in repos
            }

            for future in as_completed(future_to_repo):
                repo = future_to_repo[future]
                try:
                    prs = future.result()
                    all_prs.extend(prs)
                except Exception as e:
                    owner, name = repo
                    self._handle_api_error(e, owner, name)

        self.finalize_warnings()

        # If approved_by_me, we need to filter PRs where we actually approved
        if not include_all and approved_by_me and username:
            all_prs = self._filter_prs_approved_by_user(all_prs, username)
        # If reviewed_by_me, filter to PRs where user has reviewed but NOT approved
        elif not include_all and reviewed_by_me and username:
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
            username = self.get_username()
            if not username:
                return []

            # Use GitHub Search API
            query = f"repo:{owner}/{name} is:pr is:open assignee:{username}"

            # Add author filter if provided
            if author:
                query += f" author:{author}"

            pr_numbers = self._search_prs(query, owner, name)

            if not pr_numbers:
                return []

            result = self._fetch_prs_batch_graphql(owner, name, pr_numbers)

            result.sort(key=lambda pr: (pr.updated_at, pr.number), reverse=True)
            return result
        except Exception as e:
            self._handle_api_error(e, owner, name)
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
                    self._handle_api_error(e, owner, name)

        self.finalize_warnings()
        all_prs.sort(key=lambda pr: (pr.updated_at, pr.number), reverse=True)
        return all_prs

    @staticmethod
    def _group_prs_by_repo(prs: list[PullRequestInfo]) -> dict[tuple[str, str], list[PullRequestInfo]]:
        """Group PRs by (owner, name) tuple."""
        prs_by_repo: dict[tuple[str, str], list[PullRequestInfo]] = {}
        for pr in prs:
            key = (pr.repo_owner, pr.repo_name)
            if key not in prs_by_repo:
                prs_by_repo[key] = []
            prs_by_repo[key].append(pr)
        return prs_by_repo

    def _filter_prs_by_user_review_state(
        self,
        prs: list[PullRequestInfo],
        username: str,
        state_predicate: callable,
    ) -> list[PullRequestInfo]:
        """Filter PRs based on a user's review state using GraphQL.

        Args:
            prs: List of PRs to filter
            username: GitHub username to check review state for
            state_predicate: Function(state: str) -> bool that returns True if the state matches
        """
        if not prs:
            return []

        prs_by_repo = self._group_prs_by_repo(prs)

        matching_prs = []
        token = self._get_token()
        if not token:
            return []

        for (owner, name), repo_prs in prs_by_repo.items():
            pr_numbers = [pr.number for pr in repo_prs]

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
                data = self._post_graphql(
                    query,
                    owner=owner,
                    name=name,
                    operation='Review state filter GraphQL query',
                    token=token,
                )
                if data is None:
                    continue

                repo_data = data.get('data', {}).get('repository', {})
                if not repo_data:
                    continue

                pr_map = {pr.number: pr for pr in repo_prs}
                for i, pr_num in enumerate(pr_numbers[:100]):
                    pr_data = repo_data.get(f'pr{i}')
                    if not pr_data:
                        continue

                    reviews = pr_data.get('reviews', {}).get('nodes', [])
                    latest_review_by_user = self._compute_latest_review_states(reviews)

                    if username in latest_review_by_user:
                        user_state = latest_review_by_user[username][0]
                        if state_predicate(user_state) and pr_num in pr_map:
                            matching_prs.append(pr_map[pr_num])

            except Exception:
                pass

        return matching_prs

    def _filter_prs_approved_by_user(self, prs: list[PullRequestInfo], username: str) -> list[PullRequestInfo]:
        """Filter PRs to only include those approved by the given user."""
        return self._filter_prs_by_user_review_state(
            prs, username, lambda state: state == 'APPROVED'
        )

    def _filter_prs_reviewed_not_approved_by_user(
        self, prs: list[PullRequestInfo], username: str
    ) -> list[PullRequestInfo]:
        """Filter PRs to only include those reviewed (but not approved) by the given user."""
        return self._filter_prs_by_user_review_state(
            prs, username, lambda state: state in ('COMMENTED', 'CHANGES_REQUESTED')
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
        empty_result = {
            'reviews_given': 0,
            'reviews_received': 0,
            'avg_turnaround_hours': 0.0,
            'top_reviewers': [],
            'top_reviewed_by': [],
        }
        if not repos or not username:
            return empty_result

        token = self._get_token()
        if not token:
            return empty_result

        from datetime import datetime, timedelta, timezone
        from collections import defaultdict

        if days == -1:
            cutoff_iso = '2000-01-01T00:00:00Z'
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            cutoff_iso = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')

        reviews_given_to = defaultdict(lambda: {'count': 0, 'avatar': ''})  # username -> {count, avatar}
        reviews_received_from = defaultdict(lambda: {'count': 0, 'avatar': ''})  # username -> {count, avatar}
        reviews_given_count = 0
        reviews_received_count = 0
        total_turnaround = 0.0
        turnaround_count = 0

        # GitHub Search API caps results at 1000; page through in batches of 100.
        SEARCH_PAGE_SIZE = 100
        MAX_SEARCH_RESULTS = 1000

        def _paginate_search(
            owner: str, name: str, search_query: str, node_fields: str, operation: str
        ) -> list:
            """Page through a GraphQL search, returning all PullRequest nodes."""
            nodes: list = []
            cursor = None
            while len(nodes) < MAX_SEARCH_RESULTS:
                after = f', after: "{cursor}"' if cursor else ''
                query = f'''
                    query {{
                        search(
                            query: "{search_query}",
                            type: ISSUE, first: {SEARCH_PAGE_SIZE}{after}
                        ) {{
                            pageInfo {{ hasNextPage endCursor }}
                            nodes {{
                                ... on PullRequest {{
                                    {node_fields}
                                }}
                            }}
                        }}
                    }}
                '''
                data = self._post_graphql(
                    query, owner=owner, name=name, operation=operation, token=token,
                )
                if data is None:
                    break
                search = data.get('data', {}).get('search', {})
                nodes.extend(search.get('nodes', []))
                page_info = search.get('pageInfo', {})
                if not page_info.get('hasNextPage'):
                    break
                cursor = page_info.get('endCursor')
                if not cursor:
                    break
            if len(nodes) >= MAX_SEARCH_RESULTS:
                logger.warning(
                    "%s hit the %d-result cap for %s/%s; review stats may be truncated",
                    operation, MAX_SEARCH_RESULTS, owner, name,
                )
            return nodes

        received_fields = '''
            number
            createdAt
            reviews(first: 100) {
                nodes {
                    author { login avatarUrl }
                    state
                    submittedAt
                }
            }
        '''

        given_fields = '''
            number
            author { login avatarUrl }
            reviews(first: 100) {
                nodes {
                    author { login }
                    state
                    submittedAt
                }
            }
        '''

        def _fetch_received(owner: str, name: str) -> list:
            return _paginate_search(
                owner, name,
                f"repo:{owner}/{name} is:pr author:{username} updated:>{cutoff_iso[:10]}",
                received_fields,
                'Review stats received GraphQL query',
            )

        def _fetch_given(owner: str, name: str) -> list:
            return _paginate_search(
                owner, name,
                f"repo:{owner}/{name} is:pr reviewed-by:{username} updated:>{cutoff_iso[:10]}",
                given_fields,
                'Review stats given GraphQL query',
            )

        # Fetch both searches for every repo in parallel (network-bound); aggregate
        # single-threaded below so the shared counters/dicts stay race-free.
        received_by_repo: dict[tuple[str, str], list] = {}
        given_by_repo: dict[tuple[str, str], list] = {}
        with ThreadPoolExecutor(max_workers=min(10, len(repos) * 2)) as executor:
            future_map = {}
            for owner, name in repos:
                future_map[executor.submit(_fetch_received, owner, name)] = ('received', owner, name)
                future_map[executor.submit(_fetch_given, owner, name)] = ('given', owner, name)
            for future in as_completed(future_map):
                kind, owner, name = future_map[future]
                try:
                    result = future.result()
                except Exception:
                    result = []
                if kind == 'received':
                    received_by_repo[(owner, name)] = result
                else:
                    given_by_repo[(owner, name)] = result

        # Aggregate reviews received (PRs the user authored)
        for prs in received_by_repo.values():
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

        # Aggregate reviews given (PRs the user reviewed)
        for prs in given_by_repo.values():
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
