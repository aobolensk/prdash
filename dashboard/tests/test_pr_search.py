from dataclasses import asdict
from datetime import datetime, timezone

from django.template import Context, Template
from django.test import SimpleTestCase

from dashboard.github_client import CIStatus, LinkedIssue, PullRequestInfo, ReviewStatus
from prdash import pr_search


def make_pr(**overrides):
    values = {
        'number': 42,
        'title': 'Fix scheduler deadlock',
        'url': 'https://github.com/org/core/pull/42',
        'repo_owner': 'org',
        'repo_name': 'core',
        'author': 'Alice',
        'author_avatar': '',
        'created_at': datetime(2026, 1, 1, tzinfo=timezone.utc),
        'updated_at': datetime(2026, 1, 2, tzinfo=timezone.utc),
        'labels': [{'name': 'Good First Issue', 'color': '00ff00'}, {'name': 'bug', 'color': 'ff0000'}],
        'ci_status': CIStatus(state='success'),
        'review_status': ReviewStatus(state='not_reviewed'),
        'draft': False,
        'additions': 1,
        'deletions': 1,
        'branch_name': 'fix/sched',
        'linked_issues': [LinkedIssue(number=7, url='https://github.com/org/core/issues/7')],
    }
    values.update(overrides)
    return PullRequestInfo(**values)


def state(text='', pills=(), exclude_text='', exclude_pills=(), alternatives=(), open_=True):
    return {
        'open': open_,
        'include': {'text': text, 'pills': [{'kind': kind, 'value': value} for kind, value in pills]},
        'exclude': {'text': exclude_text, 'pills': [{'kind': kind, 'value': value} for kind, value in exclude_pills]},
        'alternatives': list(alternatives),
    }


class SearchHaystackTests(SimpleTestCase):
    # The data-search markup _pr_card.html rendered before it used the pr_search_haystack filter.
    LEGACY_TEMPLATE = (
        '{{ pr.title }} {{ pr.repo_full_name }} #{{ pr.number }} '
        '{% if pr.author %}{{ pr.author }} {% endif %}'
        '{% if pr.branch_name %}{{ pr.branch_name }} {% endif %}'
        '{% for label in pr.labels %}{{ label.name }} {% endfor %}'
        '{% for issue in pr.linked_issues %}#{{ issue.number }} {% endfor %}'
    )

    def test_haystack_matches_previous_card_markup(self):
        for pr in (make_pr(), make_pr(branch_name='', labels=[], linked_issues=[], title='<b>&"x"')):
            legacy = Template(self.LEGACY_TEMPLATE).render(Context({'pr': pr}))
            rendered = Template('{% load dashboard_tags %}{{ pr|pr_search_haystack }}').render(Context({'pr': pr}))
            self.assertEqual(rendered, legacy)

    def test_haystack_is_the_same_for_hook_dicts(self):
        pr = make_pr()
        as_dict = asdict(pr)

        self.assertEqual(pr_search.search_haystack(as_dict), pr_search.search_haystack(pr))
        self.assertTrue(pr_search.matches(as_dict, state(pills=[('repo', 'org/core')])))


class SearchMatchTests(SimpleTestCase):
    def test_closed_or_empty_search_matches_everything(self):
        pr = make_pr()

        self.assertTrue(pr_search.matches(pr, state('nothing-like-this', open_=False)))
        self.assertTrue(pr_search.matches(pr, state()))
        self.assertTrue(pr_search.matches(pr, None))
        self.assertFalse(pr_search.is_active(state('deadlock', open_=False)))

    def test_text_tokens_match_substrings_and_typos_within_budget(self):
        pr = make_pr()

        self.assertTrue(pr_search.matches(pr, state('SCHED dead')))
        self.assertTrue(pr_search.matches(pr, state('deadlok')))
        self.assertTrue(pr_search.matches(pr, state('#7')))
        self.assertFalse(pr_search.matches(pr, state('dxx')))
        self.assertFalse(pr_search.matches(pr, state('deadlock missing')))

    def test_typo_budget_scales_with_token_length(self):
        self.assertEqual([pr_search.max_typos(n) for n in (3, 4, 6, 7, 10, 11)], [0, 1, 1, 2, 2, 3])
        self.assertEqual(pr_search.fuzzy_best_distance('scheduler', 'shed'), 1)
        self.assertEqual(pr_search.fuzzy_best_distance('', 'abc'), float('inf'))

    def test_pills_match_their_field_exactly(self):
        pr = make_pr()

        self.assertTrue(pr_search.matches(pr, state(pills=[('label', 'good first issue')])))
        self.assertFalse(pr_search.matches(pr, state(pills=[('label', 'good')])))
        self.assertTrue(pr_search.matches(pr, state(pills=[('repo', 'ORG/core'), ('author', 'alice')])))
        self.assertFalse(pr_search.matches(pr, state(pills=[('repo', 'org/cor')])))

    def test_exclude_tokens_rule_out_matches(self):
        pr = make_pr()

        self.assertFalse(pr_search.matches(pr, state('deadlock', exclude_pills=[('label', 'bug')])))
        self.assertFalse(pr_search.matches(pr, state(exclude_text='scheduler')))
        self.assertTrue(pr_search.matches(pr, state(exclude_text='docs')))

    def test_alternatives_are_ored(self):
        pr = make_pr()
        docs_group = {'include': {'text': 'docs', 'pills': []}, 'exclude': {'text': '', 'pills': []}}
        bug_group = {'include': {'text': '', 'pills': [{'kind': 'label', 'value': 'bug'}]},
                     'exclude': {'text': '', 'pills': []}}

        self.assertFalse(pr_search.matches(pr, state('docs')))
        self.assertTrue(pr_search.matches(pr, state('docs', alternatives=[docs_group, bug_group])))
        self.assertTrue(pr_search.matches(pr, state(alternatives=[bug_group])))

    def test_invalid_pills_are_ignored(self):
        bad = state()
        bad['include']['pills'] = [{'kind': 'milestone', 'value': 'v1'}, {'kind': 'label', 'value': '  '}, 'junk']

        self.assertTrue(pr_search.matches(make_pr(), bad))
