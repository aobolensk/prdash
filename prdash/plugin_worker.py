"""Entry point run in each plugin's isolated worker process.

Invoked as `python -m prdash.plugin_worker`. Never imports Django or the
`dashboard` package: that is what makes this an isolation boundary rather
than a convention. All communication with the host happens over stdin/stdout
using the framing defined in prdash.plugin_protocol.
"""

from dataclasses import asdict, dataclass, field, is_dataclass
from importlib import import_module, resources
import sys
from typing import Mapping

from prdash.plugin_api import (
    PR_LIST_QUERY_HOOK,
    PluginJsonResponse,
    PluginNoContent,
    PluginRedirect,
    PluginTemplateResponse,
    PluginUserData,
    PullRequestListContext,
    PullRequestQuery,
    RequestInfo,
)
from prdash.plugin_protocol import ProtocolError, read_message, write_message


@dataclass
class _Registration:
    hooks: list = field(default_factory=list)
    ui: list = field(default_factory=list)
    routes: dict = field(default_factory=dict)
    services: dict = field(default_factory=dict)


class _Connection:
    """Duplex message channel: sends callbacks to the host, mid-call."""

    def __init__(self, stdin, stdout):
        self._stdin = stdin
        self._stdout = stdout
        self._next_id = 0

    def callback(self, op, params):
        self._next_id += 1
        write_message(self._stdout, {
            'type': 'callback',
            'id': self._next_id,
            'op': op,
            'params': params,
        })
        message = read_message(self._stdin)
        if message.get('type') != 'callback_result':
            raise ProtocolError(f'Expected callback_result, got {message.get("type")!r}')
        if not message.get('ok', False):
            raise RuntimeError(message.get('error') or 'Plugin host callback failed')
        return message.get('result', {})


class _WorkerRegistrar:
    def __init__(self, conn, plugin_id, registration, deployment_config):
        self._conn = conn
        self._plugin_id = plugin_id
        self._registration = registration
        self._deployment_config = deployment_config

    @property
    def deployment_config(self):
        return dict(self._deployment_config)

    def register_hook(self, name, callback, *, priority=100):
        if not name or not callable(callback):
            raise ValueError('Plugin hooks require a name and callable')
        self._registration.hooks.append((name, priority, callback))

    def register_ui(self, contribution):
        self._registration.ui.append(contribution)

    def register_route(self, name, callback):
        if not callable(callback):
            raise ValueError('Plugin routes require a callable')
        if name in self._registration.routes:
            raise ValueError(f'Duplicate plugin route: {name}')
        self._registration.routes[name] = callback

    def register_service(self, name, handler):
        if not name or not callable(handler):
            raise ValueError('Plugin services require a name and callable')
        if name in self._registration.services:
            raise ValueError(f'Duplicate plugin service: {name}')
        self._registration.services[name] = handler

    def call_service(self, user_id, plugin_id, name, args):
        result = self._conn.callback('call_service', {
            'user_id': user_id,
            'plugin_id': plugin_id,
            'name': name,
            'args': dict(args),
        })
        return result.get('result')

    def resolve_github_token(self, user_id):
        result = self._conn.callback('resolve_github_token', {'user_id': user_id})
        return result.get('token')

    def list_tracked_repositories(self, user_id, *, enabled_only=False):
        result = self._conn.callback('list_tracked_repositories', {
            'user_id': user_id, 'enabled_only': enabled_only,
        })
        return tuple(result['repos'])

    def get_username(self, user_id):
        result = self._conn.callback('get_username', {'user_id': user_id})
        return result.get('username')

    def fetch_open_prs(self, user_id, repos, author=None):
        result = self._conn.callback('fetch_open_prs', {
            'user_id': user_id, 'repos': [list(repo) for repo in repos], 'author': author,
        })
        return tuple(result['prs'])

    def fetch_merged_prs(self, user_id, repos, author=None):
        result = self._conn.callback('fetch_merged_prs', {
            'user_id': user_id, 'repos': [list(repo) for repo in repos], 'author': author,
        })
        return tuple(result['prs'])

    def fetch_reviews_for_stats(self, user_id, repos, username, days=30):
        return self._conn.callback('fetch_reviews_for_stats', {
            'user_id': user_id,
            'repos': [list(repo) for repo in repos],
            'username': username,
            'days': days,
        })

    def get_user_config(self, user_id):
        result = self._conn.callback('get_user_config', {'user_id': user_id})
        return result['config']

    def update_user_config(self, user_id, values):
        self._conn.callback('update_user_config', {'user_id': user_id, 'values': dict(values)})

    def list_user_data(self, user_id, collection):
        result = self._conn.callback('list_user_data', {'user_id': user_id, 'collection': collection})
        return tuple(_to_plugin_user_data(item) for item in result['items'])

    def get_user_data(self, user_id, collection, key):
        result = self._conn.callback(
            'get_user_data', {'user_id': user_id, 'collection': collection, 'key': key}
        )
        item = result.get('item')
        return _to_plugin_user_data(item) if item else None

    def set_user_data(self, user_id, collection, key, value):
        result = self._conn.callback('set_user_data', {
            'user_id': user_id, 'collection': collection, 'key': key, 'value': value,
        })
        return _to_plugin_user_data(result['item'])

    def delete_user_data(self, user_id, collection, key):
        result = self._conn.callback(
            'delete_user_data', {'user_id': user_id, 'collection': collection, 'key': key}
        )
        return result['deleted']

    def reorder_user_data(self, user_id, collection, keys):
        self._conn.callback(
            'reorder_user_data', {'user_id': user_id, 'collection': collection, 'keys': list(keys)}
        )


def _to_plugin_user_data(item):
    return PluginUserData(item['key'], item['value'], item['created_at'], item['updated_at'])


def _load_object(target):
    module_name, separator, attribute = target.partition(':')
    if not separator or not module_name or not attribute:
        raise ValueError(f'Invalid plugin entry point: {target}')
    return getattr(import_module(module_name), attribute)


def _materialize(candidate):
    if isinstance(candidate, type):
        return candidate()
    if callable(candidate) and not hasattr(candidate, 'initialize'):
        return candidate()
    return candidate


def _initialize(conn, params):
    plugin_id = params['plugin_id']
    python_path = params.get('python_path')
    if python_path:
        sys.path.insert(0, python_path)
    candidate = _load_object(params['entrypoint'])
    plugin = _materialize(candidate)
    registration = _Registration()
    registrar = _WorkerRegistrar(conn, plugin_id, registration, params.get('deployment_config', {}))
    plugin.initialize(registrar)
    return plugin, registration


def _describe(plugin, registration):
    metadata = plugin.metadata
    ui = []
    for contribution in registration.ui:
        source = resources.files(contribution.template.package).joinpath(
            contribution.template.path
        ).read_text(encoding='utf-8')
        ui.append({
            'slot': contribution.slot,
            'order': contribution.order,
            'has_context_provider': contribution.context_provider is not None,
            'template_source': source,
        })
    return {
        'metadata': {
            'plugin_id': metadata.plugin_id,
            'name': metadata.name,
            'version': metadata.version,
            'api_version': metadata.api_version,
            'description': metadata.description,
            'dependencies': [
                {'plugin_id': dep.plugin_id, 'version': dep.version}
                for dep in metadata.dependencies
            ],
        },
        'hooks': [{'name': name, 'priority': priority} for name, priority, _cb in registration.hooks],
        'routes': list(registration.routes.keys()),
        'services': list(registration.services.keys()),
        'ui': ui,
    }


def _build_request_info(data):
    if data is None:
        return None
    return RequestInfo(**data)


def _build_hook_context(data):
    query = data.get('query')
    current_repo = data.get('current_repo')
    return PullRequestListContext(
        request=_build_request_info(data.get('request')),
        active_tab=data['active_tab'],
        current_username=data.get('current_username'),
        current_repo=tuple(current_repo) if current_repo else None,
        query_defaults=data.get('query_defaults', {}),
        query=PullRequestQuery(**query) if query else None,
    )


def _invoke_hook(registration, params):
    name = params['name']
    value = params['value']
    config = params.get('config', {})
    context = _build_hook_context(params['context'])
    if name == PR_LIST_QUERY_HOOK:
        value = PullRequestQuery(**value)
    matches = [entry for entry in registration.hooks if entry[0] == name]
    for _name, _priority, callback in sorted(matches, key=lambda entry: entry[1]):
        result = callback(value, context, config)
        if result is not None:
            value = result
    if is_dataclass(value):
        value = asdict(value)
    return {'value': value}


def _read_template_source(template):
    return resources.files(template.package).joinpath(template.path).read_text(encoding='utf-8')


def _serialize_response(response):
    if isinstance(response, PluginTemplateResponse):
        return {
            'type': 'template',
            'source': _read_template_source(response.template),
            'context': dict(response.context),
            'status': response.status,
            'nested': {
                key: {
                    'source': _read_template_source(nested.template),
                    'context': dict(nested.context),
                }
                for key, nested in response.nested.items()
            },
        }
    if isinstance(response, PluginJsonResponse):
        return {
            'type': 'json',
            'data': response.data,
            'status': response.status,
            'headers': dict(response.headers),
        }
    if isinstance(response, PluginRedirect):
        return {'type': 'redirect', 'url': response.url, 'permanent': response.permanent}
    if isinstance(response, PluginNoContent):
        return {'type': 'no_content', 'status': response.status, 'headers': dict(response.headers)}
    raise TypeError(
        'Plugin routes must return PluginTemplateResponse, PluginJsonResponse, '
        'PluginRedirect, or PluginNoContent'
    )


def _invoke_route(registration, params):
    callback = registration.routes.get(params['route'])
    if callback is None:
        return {'response': {'type': 'not_found'}}
    request = _build_request_info(params['request'])
    config = params.get('config', {})
    response = callback(request, config)
    return {'response': _serialize_response(response)}


def _invoke_ui_context(registration, params):
    contribution = registration.ui[params['index']]
    if contribution.context_provider is None:
        return {'context': {}}
    request = _build_request_info(params['request'])
    config = params.get('config', {})
    context = contribution.context_provider(request, config)
    if not isinstance(context, Mapping):
        raise TypeError('Plugin UI context providers must return a mapping')
    return {'context': dict(context)}


def _invoke_service(registration, params):
    handler = registration.services.get(params['name'])
    if handler is None:
        raise KeyError(f'Unknown service: {params["name"]}')
    return {'result': handler(params.get('args', {}))}


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    conn = _Connection(stdin, stdout)
    plugin = None
    registration = None

    while True:
        try:
            message = read_message(stdin)
        except ProtocolError:
            return
        if message.get('type') != 'call':
            continue

        call_id = message['id']
        op = message['op']
        params = message.get('params', {})
        try:
            if op == 'initialize':
                plugin, registration = _initialize(conn, params)
                result = _describe(plugin, registration)
            elif op == 'invoke_hook':
                result = _invoke_hook(registration, params)
            elif op == 'invoke_route':
                result = _invoke_route(registration, params)
            elif op == 'invoke_ui_context':
                result = _invoke_ui_context(registration, params)
            elif op == 'invoke_service':
                result = _invoke_service(registration, params)
            elif op == 'shutdown':
                if plugin is not None:
                    plugin.shutdown()
                write_message(stdout, {'type': 'result', 'id': call_id, 'ok': True, 'result': {}})
                return
            else:
                raise ValueError(f'Unknown op: {op}')
            write_message(stdout, {'type': 'result', 'id': call_id, 'ok': True, 'result': result})
        except Exception as error:
            write_message(stdout, {
                'type': 'result',
                'id': call_id,
                'ok': False,
                'error': str(error) or error.__class__.__name__,
            })


if __name__ == '__main__':
    main()
