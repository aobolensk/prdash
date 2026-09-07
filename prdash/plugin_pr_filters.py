"""Pure in-memory pull request filters available to plugin workers.

These operate only on already-fetched PullRequestInfo-shaped dicts (as sent
across the hook boundary), so they need no network or database access and can
run inside a plugin's isolated worker process.
"""


def _filter_by_user_review_state(prs, username, state_predicate):
    return [
        pr for pr in prs
        if state_predicate(pr.get('review_status', {}).get('review_states', {}).get(username))
    ]


def filter_prs_approved_by_user(prs, username):
    """Filter PRs to only include those approved by the given user."""
    return _filter_by_user_review_state(prs, username, lambda state: state == 'APPROVED')


def filter_prs_reviewed_not_approved_by_user(prs, username):
    """Filter PRs to only include those reviewed (but not approved) by the given user."""
    return _filter_by_user_review_state(
        prs, username, lambda state: state in ('COMMENTED', 'CHANGES_REQUESTED')
    )
