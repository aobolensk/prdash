from prdash.plugin_api import (
    PLUGIN_API_VERSION,
    PR_LIST_FILTERS_SLOT,
    PR_LIST_PROCESS_HOOK,
    PR_LIST_QUERY_HOOK,
    PluginMetadata,
    TemplateResource,
    UIContribution,
)


FILTER_KEYS = ('ci', 'review', 'my_review', 'draft', 'conflicts')


class PullRequestFiltersPlugin:
    metadata = PluginMetadata(
        plugin_id='pull-request-filters',
        name='Pull Request Filters',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
        description=(
            'Filter and sort pull requests by author, review, CI, draft, and merge state.'
        ),
    )

    def initialize(self, registrar):
        registrar.register_hook(PR_LIST_QUERY_HOOK, self.prepare_query)
        registrar.register_hook(PR_LIST_PROCESS_HOOK, self.process_results)
        registrar.register_ui(UIContribution(
            slot=PR_LIST_FILTERS_SLOT,
            template=TemplateResource(
                package='prdash_pull_request_filters',
                path='templates/filters.html',
            ),
        ))

    def shutdown(self):
        pass

    @staticmethod
    def prepare_query(query, context, config):
        defaults = context.query_defaults
        parameters = {
            'ci': context.request.GET.get('ci', defaults.get('ci', '')),
            'review': context.request.GET.get('review', defaults.get('review', '')),
            'my_review': context.request.GET.get(
                'my_review',
                defaults.get('my_review', ''),
            ),
            'draft': context.request.GET.get('draft', defaults.get('draft', '')),
            'conflicts': context.request.GET.get(
                'conflicts',
                defaults.get('conflicts', ''),
            ),
            'sort': context.request.GET.get(
                'sort',
                defaults.get('sort', 'updated_desc'),
            ),
            'author': context.request.GET.get(
                'author',
                defaults.get('author', ''),
            ).strip(),
        }
        query.parameters.update(parameters)

        author = parameters['author']
        if author:
            query.fetch_options['author'] = author
        query.cache_vary['author'] = author

        my_review = parameters['my_review']
        if context.active_tab == 'review_requests':
            query.cache_vary['my_review'] = my_review
            review_options = {
                'pending': {
                    'include_all': False,
                    'approved_by_me': False,
                    'reviewed_by_me': False,
                },
                'approved': {
                    'include_all': False,
                    'approved_by_me': True,
                    'reviewed_by_me': False,
                },
                'reviewed': {
                    'include_all': False,
                    'approved_by_me': False,
                    'reviewed_by_me': True,
                },
            }
            query.fetch_options.update(review_options.get(my_review, {}))

        query.affects_count = bool(
            author or any(parameters[key] for key in FILTER_KEYS)
        )
        return query

    @staticmethod
    def process_results(pull_requests, context, config):
        parameters = context.query.parameters
        pull_requests = list(pull_requests)

        if context.current_repo and context.active_tab == 'review_requests':
            my_review = parameters.get('my_review')
            if my_review == 'approved' and context.current_username:
                pull_requests = context.client.filter_prs_approved_by_user(
                    pull_requests,
                    context.current_username,
                )
            elif my_review == 'reviewed' and context.current_username:
                pull_requests = context.client.filter_prs_reviewed_not_approved_by_user(
                    pull_requests,
                    context.current_username,
                )

        ci = parameters.get('ci')
        review = parameters.get('review')
        draft = parameters.get('draft')
        conflicts = parameters.get('conflicts')
        sort = parameters.get('sort', 'updated_desc')

        if ci:
            pull_requests = [
                pull_request
                for pull_request in pull_requests
                if pull_request.ci_status.state == ci
            ]
        if review:
            pull_requests = [
                pull_request
                for pull_request in pull_requests
                if pull_request.review_status.state == review
            ]
        if draft == 'ready':
            pull_requests = [
                pull_request for pull_request in pull_requests if not pull_request.draft
            ]
        elif draft == 'draft':
            pull_requests = [
                pull_request for pull_request in pull_requests if pull_request.draft
            ]
        if conflicts == 'has':
            pull_requests = [
                pull_request
                for pull_request in pull_requests
                if pull_request.mergeable == 'CONFLICTING'
            ]
        elif conflicts == 'none':
            pull_requests = [
                pull_request
                for pull_request in pull_requests
                if pull_request.mergeable == 'MERGEABLE'
            ]

        sort_keys = {
            'updated': lambda pull_request: (
                pull_request.updated_at,
                pull_request.repo_owner,
                pull_request.repo_name,
                pull_request.number,
            ),
            'created': lambda pull_request: (
                pull_request.created_at,
                pull_request.repo_owner,
                pull_request.repo_name,
                pull_request.number,
            ),
        }
        sort_field = sort.replace('_desc', '').replace('_asc', '')
        sort_key = sort_keys.get(sort_field, sort_keys['updated'])
        reverse = sort.endswith('_desc') or not sort.endswith('_asc')
        return sorted(pull_requests, key=sort_key, reverse=reverse)


plugin = PullRequestFiltersPlugin()
