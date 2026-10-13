"""Server-side port of the PR search bar matching in dashboard.js (applyPrSearch).

`state` is the search bar state returned by getPrSearchState() in the browser:
{open, include: {text, pills: [{kind, value}]}, exclude: {...}, alternatives: [{include, exclude}]}.
PRs may be PullRequestInfo objects or the PullRequestInfo-shaped dicts plugin hooks receive.
"""

import json
import math

PILL_KINDS = ('repo', 'label', 'author')


def _get(item, name, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _repo_full_name(pr):
    # Hook dicts come from asdict(), which drops the repo_full_name property.
    return _get(pr, 'repo_full_name') or f"{_get(pr, 'repo_owner', '')}/{_get(pr, 'repo_name', '')}"


def search_haystack(pr):
    """The text a PR card is searched by, rendered into its data-search attribute."""
    parts = [f"{_get(pr, 'title', '')} {_repo_full_name(pr)} #{_get(pr, 'number', '')} "]
    if _get(pr, 'author'):
        parts.append(f"{_get(pr, 'author')} ")
    if _get(pr, 'branch_name'):
        parts.append(f"{_get(pr, 'branch_name')} ")
    parts.extend(f"{_get(label, 'name', '')} " for label in _get(pr, 'labels', None) or ())
    parts.extend(f"#{_get(issue, 'number', '')} " for issue in _get(pr, 'linked_issues', None) or ())
    return ''.join(parts)


def max_typos(length):
    """Typo budget for a token, short tokens must match exactly (prMaxTypos)."""
    if length < 4:
        return 0
    if length <= 6:
        return 1
    if length <= 10:
        return 2
    return 3


def fuzzy_best_distance(text, token):
    """Smallest edit distance of token to any substring of text (prFuzzyBestDistance)."""
    previous = [0] * (len(text) + 1)
    for i, token_char in enumerate(token, start=1):
        current = [i] + [0] * len(text)
        for j, text_char in enumerate(text, start=1):
            current[j] = min(
                previous[j - 1] + (0 if token_char == text_char else 1),
                previous[j] + 1,
                current[j - 1] + 1,
            )
        previous = current
    return min(previous[1:], default=math.inf)


def token_matches(token, haystack):
    """Whether a lowercased free-text token matches a lowercased haystack (prTokenMatches)."""
    if token in haystack:
        return True
    budget = max_typos(len(token))
    return budget > 0 and fuzzy_best_distance(haystack, token) <= budget


def _token_matches_pr(token, pr, haystack):
    kind, value = token
    if kind == 'repo':
        return _repo_full_name(pr).lower() == value
    if kind == 'label':
        return value in [(_get(label, 'name') or '').lower() for label in _get(pr, 'labels', None) or ()]
    if kind == 'author':
        return (_get(pr, 'author') or '').lower() == value
    return token_matches(value, haystack)


def _field_tokens(field):
    if not isinstance(field, dict):
        return []
    tokens = [
        (pill['kind'], pill['value'].strip().lower())
        for pill in field.get('pills') or ()
        if isinstance(pill, dict) and pill.get('kind') in PILL_KINDS
        and isinstance(pill.get('value'), str) and pill['value'].strip()
    ]
    text = field.get('text')
    if isinstance(text, str):
        tokens.extend(('text', word) for word in text.lower().split())
    return tokens


def _groups(state):
    if not isinstance(state, dict) or not state.get('open'):
        return []
    alternatives = state.get('alternatives')
    groups = [state]
    if isinstance(alternatives, list):
        groups.extend(group for group in alternatives if isinstance(group, dict))
    parsed = [(_field_tokens(group.get('include')), _field_tokens(group.get('exclude'))) for group in groups]
    return [(include, exclude) for include, exclude in parsed if include or exclude]


def is_active(state):
    """Whether the state filters anything, an inactive state matches every PR."""
    return bool(_groups(state))


def fetch_scopes(state):
    """GitHub search scopes that together find every open PR the state can match.

    None means no narrowing and [] means no PR can match. Only repo and label pills narrow:
    free text is fuzzy and an author qualifier fails the search for unknown or bot logins.
    """
    groups = _groups(state)
    if not groups:
        return None
    scopes = []
    for include, exclude in groups:
        repos = {value for kind, value in include if kind == 'repo'}
        if len(repos) > 1:
            continue
        repos -= {value for kind, value in exclude if kind == 'repo'}
        if any(kind == 'repo' for kind, _ in include) and not repos:
            continue
        qualifiers = sorted(
            {f'label:"{value}"' for kind, value in include if kind == 'label' and '"' not in value}
            | {f'-label:"{value}"' for kind, value in exclude if kind == 'label' and '"' not in value}
        )
        if not repos and not qualifiers:
            return None
        scope = {'repos': sorted(repos) or None, 'qualifiers': qualifiers}
        if scope not in scopes:
            scopes.append(scope)
    return sorted(scopes, key=lambda scope: json.dumps(scope, sort_keys=True))


def matches(pr, state):
    """Whether a PR is shown under the search bar state: groups are ORed, include tokens ANDed."""
    groups = _groups(state)
    if not groups:
        return True
    haystack = search_haystack(pr).lower()
    return any(
        all(_token_matches_pr(token, pr, haystack) for token in include)
        and not any(_token_matches_pr(token, pr, haystack) for token in exclude)
        for include, exclude in groups
    )
