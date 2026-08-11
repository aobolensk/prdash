"""Saved fuzzy-search category plugin."""

import json

from django.http import HttpResponseNotAllowed, JsonResponse

from prdash.plugin_api import (
    HEAD_SLOT,
    PLUGIN_API_VERSION,
    PR_LIST_FILTERS_SLOT,
    PluginMetadata,
    TemplateResource,
    UIContribution,
)


CATEGORY_COLLECTION = 'categories'
MAX_CATEGORY_NAME_LENGTH = 80
MAX_TEXT_LENGTH = 500
MAX_PILLS_PER_FIELD = 40
MAX_PILL_VALUE_LENGTH = 200
PILL_KINDS = {'repo', 'label'}


class SavedViewsPlugin:
    metadata = PluginMetadata(
        plugin_id='saved-views',
        name='Saved Views',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
        description='Save fuzzy pull request searches as one-click views.',
    )

    def initialize(self, registrar):
        self.registrar = registrar
        registrar.register_ui(UIContribution(
            slot=HEAD_SLOT,
            template=TemplateResource(
                package='prdash_saved_views',
                path='templates/head.html',
            ),
        ))
        registrar.register_ui(UIContribution(
            slot=PR_LIST_FILTERS_SLOT,
            template=TemplateResource(
                package='prdash_saved_views',
                path='templates/categories.html',
            ),
            context_provider=self.categories_context,
        ))
        registrar.register_route('categories', self.categories)

    def shutdown(self):
        pass

    def categories_context(self, request, config):
        categories = []
        for item in self.registrar.list_user_data(request.user, CATEGORY_COLLECTION):
            try:
                query = self._validate_query(item.value['query'])
            except (KeyError, TypeError, ValueError):
                continue
            categories.append({'name': item.key, 'query': query})
        return {'saved_search_categories': categories}

    def categories(self, request, config):
        if request.method == 'POST':
            return self.save_category(request)
        if request.method == 'DELETE':
            return self.delete_category(request)
        if request.method == 'PUT':
            return self.order_categories(request)
        if request.method in ('GET', 'HEAD'):
            return JsonResponse({'categories': self.categories_context(request, config)['saved_search_categories']})
        return HttpResponseNotAllowed(['GET', 'POST', 'PUT', 'DELETE'])

    def save_category(self, request):
        try:
            payload = self._parse_payload(request)
            name = self._validate_category_name(payload.get('name'))
            query = self._validate_query(payload.get('query'))
        except ValueError as error:
            return JsonResponse({'error': str(error)}, status=400)

        self.registrar.set_user_data(
            request.user,
            CATEGORY_COLLECTION,
            name,
            {'query': query},
        )
        return JsonResponse({'category': {'name': name, 'query': query}})

    def delete_category(self, request):
        try:
            payload = self._parse_payload(request)
            name = self._validate_category_name(payload.get('name'))
        except ValueError as error:
            return JsonResponse({'error': str(error)}, status=400)

        deleted = self.registrar.delete_user_data(request.user, CATEGORY_COLLECTION, name)
        return JsonResponse({'deleted': deleted})

    def order_categories(self, request):
        try:
            payload = self._parse_payload(request)
            names = payload.get('names')
            if not isinstance(names, list):
                raise ValueError('Category order is required')
            names = [self._validate_category_name(name) for name in names]
            self.registrar.reorder_user_data(request.user, CATEGORY_COLLECTION, names)
        except ValueError as error:
            return JsonResponse({'error': str(error)}, status=400)
        return JsonResponse({'ordered': True})

    @staticmethod
    def _parse_payload(request):
        try:
            payload = json.loads(request.body)
        except (TypeError, ValueError) as error:
            raise ValueError('Request body must contain valid JSON') from error
        if not isinstance(payload, dict):
            raise ValueError('Request body must contain a JSON object')
        return payload

    @staticmethod
    def _validate_category_name(value):
        if not isinstance(value, str):
            raise ValueError('Category name is required')
        name = value.strip()
        if not name:
            raise ValueError('Category name is required')
        if len(name) > MAX_CATEGORY_NAME_LENGTH or '\n' in name or '\r' in name:
            raise ValueError('Category name must be at most 80 characters')
        return name

    @staticmethod
    def _validate_query(value):
        if not isinstance(value, dict):
            raise ValueError('Search query is required')
        query = {
            'open': True,
            'include': SavedViewsPlugin._validate_query_field(value.get('include')),
            'exclude': SavedViewsPlugin._validate_query_field(value.get('exclude')),
        }
        if not query['include']['text'] and not query['exclude']['text'] and not (
            query['include']['pills'] or query['exclude']['pills']
        ):
            raise ValueError('Search query cannot be empty')
        return query

    @staticmethod
    def _validate_query_field(value):
        if not isinstance(value, dict):
            raise ValueError('Search query fields are required')
        text = value.get('text', '')
        pills = value.get('pills', [])
        if not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH:
            raise ValueError('Search text is invalid')
        if not isinstance(pills, list) or len(pills) > MAX_PILLS_PER_FIELD:
            raise ValueError('Search pills are invalid')
        validated_pills = []
        for pill in pills:
            if not isinstance(pill, dict):
                raise ValueError('Search pill is invalid')
            kind = pill.get('kind')
            pill_value = pill.get('value')
            if kind not in PILL_KINDS or not isinstance(pill_value, str):
                raise ValueError('Search pill is invalid')
            pill_value = pill_value.strip()
            if not pill_value or len(pill_value) > MAX_PILL_VALUE_LENGTH:
                raise ValueError('Search pill is invalid')
            validated_pills.append({'kind': kind, 'value': pill_value})
        return {'text': text, 'pills': validated_pills}


plugin = SavedViewsPlugin()
