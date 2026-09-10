"""Discovery and process isolation for prdash plugins.

Each enabled plugin runs in its own subprocess (prdash/plugin_worker.py),
started here and driven over a small JSON-over-pipes protocol
(prdash/plugin_protocol.py). The worker process never imports Django or this
package, so a plugin cannot read another plugin's (or another user's) data by
importing Django models directly: the only way in is the narrow, per-plugin
scoped callback surface implemented in _handle_callback below.
"""

from collections.abc import Mapping
from dataclasses import asdict
from importlib import metadata
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
from threading import RLock, Thread

from django.conf import settings
from django.db import OperationalError, ProgrammingError, transaction
from django.db.models import Max
from django.http import (
    Http404,
    HttpResponse,
    HttpResponsePermanentRedirect,
    HttpResponseRedirect,
    HttpResponseServerError,
    JsonResponse,
)
from django.template import engines
from django.utils.safestring import mark_safe
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from prdash.plugin_api import (
    PLUGIN_API_VERSION,
    PR_LIST_PROCESS_HOOK,
    PR_LIST_QUERY_HOOK,
    PluginDependency,
    PluginMetadata,
    PluginUserData,
    PullRequestQuery,
)
from prdash.plugin_protocol import ProtocolError, read_message, write_message

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = 'prdash.plugins'
SOURCE_MANIFEST = 'prdash-plugin.json'
IDENTIFIER_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]*$')

DEFAULT_CALL_TIMEOUT = getattr(settings, 'PRDASH_PLUGIN_CALL_TIMEOUT', 10.0)
DEFAULT_ROUTE_TIMEOUT = getattr(settings, 'PRDASH_PLUGIN_ROUTE_TIMEOUT', 120.0)
SHUTDOWN_TIMEOUT = 5.0

_SAFE_REQUEST_HEADERS = {
    'HX-Request', 'HX-Target', 'HX-Trigger', 'Content-Type', 'Accept', 'Sec-Fetch-Mode',
}


class PluginActivationError(RuntimeError):
    """A user-specific activation problem, such as a disabled dependency."""


class PluginCallError(RuntimeError):
    """A plugin worker failed to complete a call (crashed, timed out, or errored)."""


class PluginDescriptor:
    """Metadata available without starting a plugin's worker process."""

    def __init__(
        self, plugin_id, name, version, description, entrypoint, source,
        *, api_version=None, python_path=None, load_error=None,
    ):
        self.plugin_id = plugin_id
        self.name = name
        self.version = version
        self.description = description
        self.entrypoint = entrypoint
        self.source = source
        self.api_version = api_version
        self.python_path = python_path
        self.load_error = load_error


class _WorkerHandle:
    """A running plugin worker process and its registration manifest."""

    def __init__(self, plugin_id, popen):
        self.plugin_id = plugin_id
        self.popen = popen
        self.lock = RLock()
        self.metadata = None
        self.manifest = None
        self.dead = False
        self._next_id = 0

    def next_call_id(self):
        self._next_id += 1
        return self._next_id


class PluginManager:
    """Process-local registry backed by per-user activation state."""

    def __init__(self):
        self.descriptors: dict[str, PluginDescriptor] = {}
        self.discovery_errors: list[str] = []
        self._workers: dict[str, _WorkerHandle] = {}
        self._loading: set[str] = set()
        self._activation_errors: dict[tuple[frozenset[str], str], str] = {}
        self._lock = RLock()

    # -- Discovery -----------------------------------------------------

    def discover(self):
        """Discover source manifests and installed entry points without loading plugins."""
        with self._lock:
            self._shutdown_all()
            self.descriptors = {}
            self.discovery_errors = []
            self._activation_errors = {}
            self._discover_sources()
            self._discover_entry_points()
        return self.descriptors

    def _discover_sources(self):
        plugin_paths = getattr(settings, 'PRDASH_PLUGIN_PATHS', ())
        for configured_path in plugin_paths:
            root = Path(configured_path)
            if not root.exists():
                continue
            for manifest_path in sorted(root.glob(f'*/{SOURCE_MANIFEST}')):
                try:
                    data = json.loads(manifest_path.read_text(encoding='utf-8'))
                    descriptor = PluginDescriptor(
                        plugin_id=data['id'],
                        name=data['name'],
                        version=data['version'],
                        description=data.get('description', ''),
                        entrypoint=data['entrypoint'],
                        source=str(manifest_path.parent),
                        api_version=data.get('api_version'),
                        python_path=(manifest_path.parent / data.get('python_path', '.')).resolve(),
                    )
                    self._add_descriptor(descriptor)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    message = f'Invalid plugin manifest {manifest_path}: {error}'
                    logger.warning(message)
                    self.discovery_errors.append(message)

    def _discover_entry_points(self):
        for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
            distribution = entry_point.dist
            dist_metadata = distribution.metadata if distribution else {}
            descriptor = PluginDescriptor(
                plugin_id=entry_point.name,
                name=dist_metadata.get('Name', entry_point.name),
                version=distribution.version if distribution else '0',
                description=dist_metadata.get('Summary', ''),
                entrypoint=entry_point.value,
                source=f'Python distribution {dist_metadata.get("Name", entry_point.name)}',
            )
            self._add_descriptor(descriptor, source_precedence=False)

    def _add_descriptor(self, descriptor, *, source_precedence=True):
        if not IDENTIFIER_PATTERN.fullmatch(descriptor.plugin_id):
            raise ValueError(f'Invalid plugin id: {descriptor.plugin_id!r}')
        existing = self.descriptors.get(descriptor.plugin_id)
        if existing:
            if source_precedence:
                message = f'Duplicate plugin id {descriptor.plugin_id!r}'
                logger.warning(message)
                self.discovery_errors.append(message)
            return
        self.descriptors[descriptor.plugin_id] = descriptor

    @staticmethod
    def _api_compatible(required):
        if not required:
            return True
        return required.split('.', 1)[0] == PLUGIN_API_VERSION.split('.', 1)[0]

    # -- Worker process lifecycle ---------------------------------------

    @staticmethod
    def _spawn_worker(descriptor):
        popen = subprocess.Popen(
            [sys.executable, '-m', 'prdash.plugin_worker'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        PluginManager._drain_stderr(descriptor.plugin_id, popen.stderr)
        return _WorkerHandle(descriptor.plugin_id, popen)

    @staticmethod
    def _drain_stderr(plugin_id, stderr):
        def run():
            for line in iter(stderr.readline, b''):
                logger.warning('Plugin %s stderr: %s', plugin_id, line.decode('utf-8', errors='replace').rstrip())
            stderr.close()

        Thread(target=run, daemon=True).start()

    def _terminate_worker(self, worker):
        if worker.popen.poll() is not None:
            return
        try:
            with worker.lock:
                worker.dead = True
                try:
                    self._call(worker, 'shutdown', {}, timeout=SHUTDOWN_TIMEOUT)
                except PluginCallError:
                    pass
        finally:
            worker.popen.terminate()
            try:
                worker.popen.wait(timeout=SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                worker.popen.kill()
                worker.popen.wait(timeout=SHUTDOWN_TIMEOUT)

    def _call_loop(self, worker, call_id, timeout):
        """Yield stream_chunks, result via StopIteration.value. Timeout resets per message: max-silence, not total."""
        while True:
            message = read_message(worker.popen.stdout, timeout=timeout)
            msg_type = message.get('type')
            if msg_type == 'result':
                if message.get('id') != call_id:
                    raise ProtocolError('Plugin worker response id mismatch')
                if not message.get('ok', False):
                    raise PluginCallError(message.get('error') or 'Plugin call failed')
                return message.get('result', {})
            if msg_type == 'callback':
                self._service_callback(worker, message)
                continue
            if msg_type == 'stream_chunk':
                yield message.get('chunk', {})
                continue
            raise ProtocolError(f'Unexpected plugin worker message type: {msg_type!r}')

    def _call(self, worker, op, params, timeout=DEFAULT_CALL_TIMEOUT):
        with worker.lock:
            if worker.dead:
                raise PluginCallError('Plugin worker is no longer running')
            call_id = worker.next_call_id()
            try:
                write_message(worker.popen.stdin, {
                    'type': 'call', 'id': call_id, 'op': op, 'params': params,
                })
                loop = self._call_loop(worker, call_id, timeout)
                while True:
                    try:
                        next(loop)
                    except StopIteration as stop:
                        return stop.value
            except (ProtocolError, OSError) as error:
                worker.dead = True
                raise PluginCallError(str(error)) from error

    def _call_streaming(self, worker, op, params, timeout=DEFAULT_CALL_TIMEOUT):
        """Like _call, but yields ('chunk', dict) per chunk then ('result', dict); holds worker.lock until the caller fully consumes or closes it."""
        with worker.lock:
            if worker.dead:
                raise PluginCallError('Plugin worker is no longer running')
            call_id = worker.next_call_id()
            try:
                write_message(worker.popen.stdin, {
                    'type': 'call', 'id': call_id, 'op': op, 'params': params,
                })
                loop = self._call_loop(worker, call_id, timeout)
                while True:
                    try:
                        chunk = next(loop)
                    except StopIteration as stop:
                        yield ('result', stop.value)
                        return
                    yield ('chunk', chunk)
            except (ProtocolError, OSError) as error:
                worker.dead = True
                raise PluginCallError(str(error)) from error

    def _service_callback(self, worker, message):
        callback_id = message.get('id')
        try:
            result = self._handle_callback(worker, message.get('op'), message.get('params') or {})
            write_message(worker.popen.stdin, {
                'type': 'callback_result', 'id': callback_id, 'ok': True, 'result': result,
            })
        except Exception as error:
            write_message(worker.popen.stdin, {
                'type': 'callback_result', 'id': callback_id, 'ok': False,
                'error': str(error) or error.__class__.__name__,
            })

    def _handle_callback(self, worker, op, params):
        """Execute one plugin-worker callback, scoped to that worker's own plugin_id."""
        plugin_id = worker.plugin_id
        if op == 'get_user_config':
            user = self._get_user(params['user_id'])
            return {'config': self.get_user_config(user, plugin_id)}
        if op == 'update_user_config':
            user = self._get_user(params['user_id'])
            self.update_user_config(user, plugin_id, params['values'])
            return {}
        if op == 'list_user_data':
            user = self._get_user(params['user_id'])
            items = self.list_user_data(user, plugin_id, params['collection'])
            return {'items': [self._user_data_to_dict(item) for item in items]}
        if op == 'get_user_data':
            user = self._get_user(params['user_id'])
            item = self.get_user_data(user, plugin_id, params['collection'], params['key'])
            return {'item': self._user_data_to_dict(item) if item else None}
        if op == 'set_user_data':
            user = self._get_user(params['user_id'])
            item = self.set_user_data(
                user, plugin_id, params['collection'], params['key'], params['value']
            )
            return {'item': self._user_data_to_dict(item)}
        if op == 'delete_user_data':
            user = self._get_user(params['user_id'])
            deleted = self.delete_user_data(user, plugin_id, params['collection'], params['key'])
            return {'deleted': deleted}
        if op == 'reorder_user_data':
            user = self._get_user(params['user_id'])
            self.reorder_user_data(user, plugin_id, params['collection'], params['keys'])
            return {}
        if op == 'resolve_github_token':
            user = self._get_user(params['user_id'])
            from .github_client import GitHubClient
            return {'token': GitHubClient(user)._get_token()}
        if op == 'list_tracked_repositories':
            user = self._get_user(params['user_id'])
            from .models import TrackedRepository
            repos = TrackedRepository.objects.filter(user=user)
            if params.get('enabled_only'):
                repos = repos.filter(enabled=True)
            return {'repos': [
                {'owner': repo.owner, 'name': repo.name, 'enabled': repo.enabled}
                for repo in repos
            ]}
        if op == 'call_service':
            user = self._get_user(params['user_id'])
            result = self.get_service(user, params['plugin_id'], params['name'], params.get('args', {}))
            return {'result': result}
        if op == 'get_username':
            from .github_client import GitHubClient
            user = self._get_user(params['user_id'])
            return {'username': GitHubClient(user).get_username()}
        if op == 'fetch_open_prs':
            from .github_client import GitHubClient
            user = self._get_user(params['user_id'])
            repos = [tuple(repo) for repo in params['repos']]
            prs = GitHubClient(user).get_all_user_prs(repos, params.get('author'))
            return {'prs': [asdict(pr) for pr in prs]}
        if op == 'fetch_merged_prs':
            from .github_client import GitHubClient
            user = self._get_user(params['user_id'])
            repos = [tuple(repo) for repo in params['repos']]
            prs = GitHubClient(user).get_all_merged_prs(repos, params.get('author'))
            return {'prs': [asdict(pr) for pr in prs]}
        if op == 'fetch_reviews_for_stats':
            from .github_client import GitHubClient
            user = self._get_user(params['user_id'])
            repos = [tuple(repo) for repo in params['repos']]
            return GitHubClient(user).get_reviews_for_stats(repos, params['username'], params.get('days', 30))
        raise ValueError(f'Unknown plugin callback: {op}')

    @staticmethod
    def _get_user(user_id):
        from django.contrib.auth.models import User
        return User.objects.get(pk=user_id)

    @staticmethod
    def _user_data_to_dict(item):
        return {
            'key': item.key,
            'value': item.value,
            'created_at': item.created_at,
            'updated_at': item.updated_at,
        }

    @staticmethod
    def _metadata_from_manifest(data):
        return PluginMetadata(
            plugin_id=data['plugin_id'],
            name=data['name'],
            version=data['version'],
            api_version=data['api_version'],
            description=data.get('description', ''),
            dependencies=tuple(
                PluginDependency(dep['plugin_id'], dep['version'])
                for dep in data.get('dependencies', ())
            ),
        )

    def load(self, plugin_id, enabled_plugin_ids):
        """Load and initialize one explicitly enabled plugin."""
        with self._lock:
            descriptor = self.descriptors.get(plugin_id)
            if descriptor is None:
                return None
            activation_key = (frozenset(enabled_plugin_ids), plugin_id)
            if activation_key in self._activation_errors:
                return None
            if descriptor.load_error:
                return None
            if not self._api_compatible(descriptor.api_version):
                descriptor.load_error = (
                    f'Requires plugin API {descriptor.api_version}, '
                    f'but prdash provides {PLUGIN_API_VERSION}'
                )
                return None

            worker = self._workers.get(plugin_id)
            if worker is not None and worker.dead:
                self._workers.pop(plugin_id, None)
                worker = None
            if worker is not None:
                try:
                    self._validate_dependencies(worker.metadata, enabled_plugin_ids)
                    for dependency in worker.metadata.dependencies:
                        if self.load(dependency.plugin_id, enabled_plugin_ids) is None:
                            raise PluginActivationError(
                                f'Dependency {dependency.plugin_id!r} could not be loaded'
                            )
                    return worker
                except PluginActivationError as error:
                    self._activation_errors[activation_key] = str(error)
                    return None

            if plugin_id in self._loading:
                self._activation_errors[activation_key] = (
                    f'Dependency cycle includes {plugin_id!r}'
                )
                return None

            worker = None
            initialization_started = False
            self._loading.add(plugin_id)
            try:
                worker = self._spawn_worker(descriptor)
                initialization_started = True
                manifest = self._call(worker, 'initialize', {
                    'plugin_id': plugin_id,
                    'entrypoint': descriptor.entrypoint,
                    'python_path': str(descriptor.python_path) if descriptor.python_path else None,
                    'deployment_config': dict(
                        getattr(settings, 'PRDASH_PLUGIN_CONFIG', {}).get(plugin_id, {})
                    ),
                })
                plugin_metadata = self._metadata_from_manifest(manifest['metadata'])
                self._validate_metadata(descriptor, plugin_metadata)
                descriptor.name = plugin_metadata.name
                descriptor.description = plugin_metadata.description
                descriptor.api_version = plugin_metadata.api_version
                self._validate_dependencies(plugin_metadata, enabled_plugin_ids)

                for dependency in plugin_metadata.dependencies:
                    dependency_worker = self.load(dependency.plugin_id, enabled_plugin_ids)
                    if dependency_worker is None:
                        raise PluginActivationError(
                            f'Dependency {dependency.plugin_id!r} could not be loaded'
                        )

                worker.metadata = plugin_metadata
                worker.manifest = manifest
                self._workers[plugin_id] = worker
                return worker
            except PluginActivationError as error:
                self._activation_errors[activation_key] = str(error)
                return None
            except Exception as error:
                descriptor.load_error = str(error) or error.__class__.__name__
                logger.exception('Failed to load plugin %s', plugin_id)
                return None
            finally:
                self._loading.discard(plugin_id)
                if initialization_started and plugin_id not in self._workers and worker is not None:
                    self._terminate_worker(worker)

    def _validate_metadata(self, descriptor, plugin_metadata):
        if not isinstance(plugin_metadata, PluginMetadata):
            raise TypeError('Plugin metadata must use PluginMetadata')
        if plugin_metadata.plugin_id != descriptor.plugin_id:
            raise ValueError(
                f'Entry point id {descriptor.plugin_id!r} does not match '
                f'plugin id {plugin_metadata.plugin_id!r}'
            )
        try:
            versions_match = Version(plugin_metadata.version) == Version(descriptor.version)
        except InvalidVersion as error:
            raise ValueError(f'Invalid plugin version: {error}') from error
        if not versions_match:
            raise ValueError(
                f'Discovered version {descriptor.version!r} does not match '
                f'plugin version {plugin_metadata.version!r}'
            )
        if not self._api_compatible(plugin_metadata.api_version):
            raise RuntimeError(
                f'Requires plugin API {plugin_metadata.api_version}, '
                f'but prdash provides {PLUGIN_API_VERSION}'
            )

    def _validate_dependencies(self, plugin_metadata, enabled_plugin_ids):
        for dependency in plugin_metadata.dependencies:
            if dependency.plugin_id not in enabled_plugin_ids:
                raise PluginActivationError(
                    f'Dependency {dependency.plugin_id!r} must be enabled first'
                )
            dependency_descriptor = self.descriptors.get(dependency.plugin_id)
            if dependency_descriptor is None:
                raise RuntimeError(f'Dependency {dependency.plugin_id!r} is not installed')
            if dependency.version:
                try:
                    matches = Version(dependency_descriptor.version) in SpecifierSet(dependency.version)
                except (InvalidSpecifier, InvalidVersion) as error:
                    raise RuntimeError(
                        f'Invalid dependency version for {dependency.plugin_id!r}: {error}'
                    ) from error
                if not matches:
                    raise RuntimeError(
                        f'Dependency {dependency.plugin_id!r} {dependency.version} is required, '
                        f'but {dependency_descriptor.version} is installed'
                    )

    def unload(self, plugin_id):
        """Shut down a loaded plugin's worker process and forget it."""
        with self._lock:
            worker = self._workers.pop(plugin_id, None)
            if worker is None:
                return
            try:
                self._terminate_worker(worker)
            except Exception:
                logger.exception('Failed to shut down plugin %s', plugin_id)

    def _shutdown_all(self):
        for plugin_id in list(self._workers):
            self.unload(plugin_id)

    # -- Per-user configuration and data ---------------------------------

    @staticmethod
    def _state_model():
        from .models import PluginConfiguration
        return PluginConfiguration

    @staticmethod
    def _user_data_model():
        from .models import PluginUserData
        return PluginUserData

    @staticmethod
    def _validate_user_data_location(collection, key=None):
        if not isinstance(collection, str) or not IDENTIFIER_PATTERN.fullmatch(collection):
            raise ValueError('Plugin data collections require a simple name')
        if key is not None and (not isinstance(key, str) or not key or len(key) > 128):
            raise ValueError('Plugin data keys must be between 1 and 128 characters')

    def _state_map(self, user, request=None):
        cache_name = '_prdash_plugin_states'
        if request is not None and hasattr(request, cache_name):
            return getattr(request, cache_name)
        if not getattr(user, 'is_authenticated', False):
            states = {}
        else:
            try:
                state_rows = self._state_model().objects.filter(user=user)
                states = {row.plugin_id: row for row in state_rows}
            except (OperationalError, ProgrammingError):
                states = {}
        if request is not None:
            setattr(request, cache_name, states)
        return states

    def _enabled_ids(self, user, request=None):
        return {
            plugin_id
            for plugin_id, state in self._state_map(user, request).items()
            if state.enabled and plugin_id in self.descriptors
        }

    def _activate_user_plugins(self, user, request=None):
        """Return user state, enabled ids, and workers loaded for this activation."""
        states = self._state_map(user, request)
        enabled_ids = self._enabled_ids(user, request)
        active_workers = {}
        for plugin_id in enabled_ids:
            worker = self.load(plugin_id, enabled_ids)
            if worker is not None:
                active_workers[plugin_id] = worker
        return states, enabled_ids, active_workers

    def configure_user(self, user, enabled_plugin_ids):
        """Persist the explicit enabled set and update this process registry."""
        selected = set(enabled_plugin_ids) & self.descriptors.keys()
        state_model = self._state_model()
        with transaction.atomic():
            enabled = set(
                state_model.objects.filter(user=user, enabled=True)
                .values_list('plugin_id', flat=True)
            ) & self.descriptors.keys()
            state_model.objects.filter(user=user).exclude(plugin_id__in=selected).update(enabled=False)
            for plugin_id in selected:
                state_model.objects.update_or_create(
                    user=user,
                    plugin_id=plugin_id,
                    defaults={'enabled': True},
                )

        for descriptor in self.descriptors.values():
            descriptor.load_error = None
        self._activation_errors = {}
        for plugin_id in selected:
            self.load(plugin_id, selected)

        for plugin_id in set(self._workers) - selected:
            if not state_model.objects.filter(plugin_id=plugin_id, enabled=True).exists():
                self.unload(plugin_id)

        return selected != enabled

    def plugin_statuses(self, user, request=None):
        states, enabled_ids, _ = self._activate_user_plugins(user, request)

        statuses = []
        for descriptor in sorted(self.descriptors.values(), key=lambda item: item.name.lower()):
            state = states.get(descriptor.plugin_id)
            activation_error = self._activation_errors.get(
                (frozenset(enabled_ids), descriptor.plugin_id)
            )
            statuses.append({
                'id': descriptor.plugin_id,
                'name': descriptor.name,
                'version': descriptor.version,
                'description': descriptor.description,
                'source': descriptor.source,
                'enabled': bool(state and state.enabled),
                'loaded': descriptor.plugin_id in self._workers,
                'error': descriptor.load_error or activation_error,
            })
        return statuses

    def get_user_config(self, user, plugin_id):
        try:
            state = self._state_model().objects.filter(
                user=user,
                plugin_id=plugin_id,
            ).first()
        except (OperationalError, ProgrammingError):
            return {}
        return dict(state.config) if state else {}

    def update_user_config(self, user, plugin_id, values):
        if plugin_id not in self.descriptors:
            raise KeyError(f'Unknown plugin: {plugin_id}')
        state, _ = self._state_model().objects.get_or_create(
            user=user,
            plugin_id=plugin_id,
        )
        state.config = dict(values)
        state.save(update_fields=['config', 'updated_at'])

    def list_user_data(self, user, plugin_id, collection):
        self._validate_user_data_location(collection)
        rows = self._user_data_model().objects.filter(
            user=user,
            plugin_id=plugin_id,
            collection=collection,
        ).order_by('position', 'key')
        return tuple(
            PluginUserData(row.key, row.value, row.created_at, row.updated_at)
            for row in rows
        )

    def get_user_data(self, user, plugin_id, collection, key):
        self._validate_user_data_location(collection, key)
        row = self._user_data_model().objects.filter(
            user=user,
            plugin_id=plugin_id,
            collection=collection,
            key=key,
        ).first()
        if row is None:
            return None
        return PluginUserData(row.key, row.value, row.created_at, row.updated_at)

    def set_user_data(self, user, plugin_id, collection, key, value):
        self._validate_user_data_location(collection, key)
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError('Plugin data values must be JSON serializable') from error
        model = self._user_data_model()
        with transaction.atomic():
            rows = model.objects.filter(
                user=user,
                plugin_id=plugin_id,
                collection=collection,
            )
            last_position = rows.aggregate(max_position=Max('position'))['max_position']
            row, _ = model.objects.update_or_create(
                user=user,
                plugin_id=plugin_id,
                collection=collection,
                key=key,
                defaults={'value': value},
                create_defaults={
                    'value': value,
                    'position': (last_position if last_position is not None else -1) + 1,
                },
            )
        return PluginUserData(row.key, row.value, row.created_at, row.updated_at)

    def delete_user_data(self, user, plugin_id, collection, key):
        self._validate_user_data_location(collection, key)
        deleted, _ = self._user_data_model().objects.filter(
            user=user,
            plugin_id=plugin_id,
            collection=collection,
            key=key,
        ).delete()
        return bool(deleted)

    def reorder_user_data(self, user, plugin_id, collection, keys):
        self._validate_user_data_location(collection)
        if not isinstance(keys, (list, tuple)):
            raise ValueError('Plugin data order must be a list of keys')
        for key in keys:
            self._validate_user_data_location(collection, key)
        if len(keys) != len(set(keys)):
            raise ValueError('Plugin data order cannot contain duplicate keys')

        model = self._user_data_model()
        with transaction.atomic():
            rows = list(model.objects.select_for_update().filter(
                user=user,
                plugin_id=plugin_id,
                collection=collection,
            ))
            rows_by_key = {row.key: row for row in rows}
            if set(keys) != set(rows_by_key):
                raise ValueError('Plugin data order must include every collection key')
            changed_rows = []
            for position, key in enumerate(keys):
                row = rows_by_key[key]
                if row.position != position:
                    row.position = position
                    changed_rows.append(row)
            if changed_rows:
                model.objects.bulk_update(changed_rows, ['position'])

    # -- Request-facing serialization -------------------------------------

    @staticmethod
    def _request_info_dict(request):
        if request is None:
            return None
        cache_name = '_prdash_plugin_request_info'
        cached = getattr(request, cache_name, None)
        if cached is not None:
            return cached
        info = PluginManager._build_request_info_dict(request)
        setattr(request, cache_name, info)
        return info

    @staticmethod
    def _build_request_info_dict(request):
        user = getattr(request, 'user', None)
        authenticated = bool(user and getattr(user, 'is_authenticated', False))
        body = ''
        if request.method in ('POST', 'PUT', 'PATCH'):
            body = request.body.decode('utf-8', errors='replace')
        return {
            'method': request.method,
            'path': request.path,
            'query_params': {key: value for key, value in request.GET.items()},
            'form_params': {key: value for key, value in request.POST.items()},
            'form_lists': {key: values for key, values in request.POST.lists()},
            'body': body,
            'headers': {
                key: value for key, value in request.headers.items()
                if key in _SAFE_REQUEST_HEADERS
            },
            'user_id': user.id if authenticated else None,
            'username': getattr(user, 'username', '') if user else '',
            'is_authenticated': authenticated,
        }

    @staticmethod
    def _hook_value_to_json(name, value):
        if name == PR_LIST_QUERY_HOOK:
            return asdict(value)
        if name == PR_LIST_PROCESS_HOOK:
            return [asdict(pr) for pr in value]
        return value

    @staticmethod
    def _hook_value_from_json(name, value):
        if name == PR_LIST_QUERY_HOOK:
            return PullRequestQuery(**value)
        if name == PR_LIST_PROCESS_HOOK:
            from .github_client import CIStatus, LinkedIssue, PullRequestInfo, ReviewStatus

            def _build(item):
                item = dict(item)
                item['ci_status'] = CIStatus(**item['ci_status'])
                item['review_status'] = ReviewStatus(**item['review_status'])
                item['linked_issues'] = [
                    LinkedIssue(**linked) for linked in item.get('linked_issues', [])
                ]
                return PullRequestInfo(**item)

            return [_build(item) for item in value]
        return value

    def _hook_context_to_json(self, hook_context):
        current_repo = hook_context.current_repo
        return {
            'request': self._request_info_dict(hook_context.request),
            'active_tab': hook_context.active_tab,
            'current_username': hook_context.current_username,
            'current_repo': list(current_repo) if current_repo else None,
            'query_defaults': dict(hook_context.query_defaults),
            'query': asdict(hook_context.query) if hook_context.query is not None else None,
        }

    # -- Hook / route / UI / service dispatch ------------------------------

    def run_hook(self, name, value, hook_context, user, request=None):
        """Run enabled hook callbacks in order and isolate individual failures."""
        states, _, active_workers = self._activate_user_plugins(user, request)

        entries = []
        for plugin_id, worker in active_workers.items():
            config = dict(states[plugin_id].config)
            for hook in worker.manifest['hooks']:
                if hook['name'] == name:
                    entries.append((hook['priority'], plugin_id, config, worker))

        context_payload = self._hook_context_to_json(hook_context)
        payload_value = self._hook_value_to_json(name, value)
        for _priority, plugin_id, config, worker in sorted(entries, key=lambda item: item[:2]):
            try:
                result = self._call(worker, 'invoke_hook', {
                    'name': name,
                    'value': payload_value,
                    'context': context_payload,
                    'config': config,
                })
                new_value = result.get('value')
                if new_value is not None:
                    payload_value = new_value
            except PluginCallError:
                logger.exception('Plugin %s failed in hook %s', plugin_id, name)
        return self._hook_value_from_json(name, payload_value)

    def render_slot(self, slot, template_context, **extra_context):
        """Render enabled UI contributions for one template slot."""
        request = template_context.get('request')
        user = getattr(request, 'user', None)
        states, _, active_workers = self._activate_user_plugins(user, request)

        contributions = []
        for plugin_id, worker in active_workers.items():
            for index, ui in enumerate(worker.manifest['ui']):
                if ui['slot'] == slot:
                    contributions.append((ui['order'], plugin_id, index, ui, worker))

        base_context = template_context.flatten()
        base_context.update(extra_context)
        request_info = self._request_info_dict(request)
        rendered = []
        for _order, plugin_id, index, ui, worker in sorted(contributions, key=lambda item: item[:3]):
            config = dict(states[plugin_id].config)
            context = dict(base_context)
            context['plugin_id'] = plugin_id
            context['plugin_config'] = config
            try:
                if ui['has_context_provider']:
                    result = self._call(worker, 'invoke_ui_context', {
                        'index': index,
                        'request': request_info,
                        'config': config,
                    })
                    provided = result.get('context', {})
                    if not isinstance(provided, Mapping):
                        raise TypeError('Plugin UI context providers must return a mapping')
                    context.update(provided)
                template = engines['django'].from_string(ui['template_source'])
                rendered.append(template.render(context, request))
            except Exception:
                logger.exception('Plugin %s failed to render slot %s', plugin_id, slot)
        return mark_safe(''.join(rendered))

    def dispatch(self, request, plugin_id, route):
        """Dispatch a request to an enabled plugin route."""
        enabled_ids = self._enabled_ids(request.user, request)
        if plugin_id not in enabled_ids:
            raise Http404
        worker = self.load(plugin_id, enabled_ids)
        if worker is None:
            return HttpResponseServerError('')
        if route not in worker.manifest['routes']:
            raise Http404
        config = dict(self._state_map(request.user, request)[plugin_id].config)
        try:
            result = self._call(worker, 'invoke_route', {
                'route': route,
                'request': self._request_info_dict(request),
                'config': config,
            }, timeout=DEFAULT_ROUTE_TIMEOUT)
            return self._response_from_payload(
                result.get('response', {}), plugin_id, config, request,
            )
        except Http404:
            raise
        except Exception:
            logger.exception('Plugin %s failed in route %s', plugin_id, route)
            return HttpResponseServerError('')

    def dispatch_stream(self, request, plugin_id, route):
        """Yield {'chunk': {...}} dicts, then exactly one {'final': True, 'response'/'error': ...} dict; all JSON serializable for SSE relay."""
        enabled_ids = self._enabled_ids(request.user, request)
        if plugin_id not in enabled_ids:
            raise Http404
        worker = self.load(plugin_id, enabled_ids)
        if worker is None:
            yield {'final': True, 'error': 'Plugin worker is not available'}
            return
        if route not in worker.manifest['routes']:
            raise Http404
        config = dict(self._state_map(request.user, request)[plugin_id].config)
        try:
            for kind, payload in self._call_streaming(worker, 'invoke_route', {
                'route': route,
                'request': self._request_info_dict(request),
                'config': config,
            }, timeout=DEFAULT_ROUTE_TIMEOUT):
                if kind == 'chunk':
                    yield {'chunk': payload}
                else:
                    response = self._stream_final_payload(
                        payload.get('response', {}), plugin_id, config, request,
                    )
                    yield {'final': True, 'response': response}
        except Http404:
            raise
        except Exception:
            logger.exception('Plugin %s failed in stream route %s', plugin_id, route)
            yield {'final': True, 'error': 'stream_failed'}

    @staticmethod
    def _render_payload(response, plugin_id, config, request):
        """Shared by _response_from_payload and _stream_final_payload; headers are excluded here since they only apply to real HttpResponse objects."""
        response_type = response.get('type')
        if response_type == 'not_found':
            return {'type': 'not_found'}
        if response_type == 'template':
            context = dict(response.get('context', {}))
            for key, nested in response.get('nested', {}).items():
                nested_template = engines['django'].from_string(nested['source'])
                context[key] = nested_template.render(dict(nested['context']), request)
            context['plugin_id'] = plugin_id
            context['plugin_config'] = config
            template = engines['django'].from_string(response['source'])
            content = template.render(context, request)
            return {'type': 'html', 'content': content, 'status': response.get('status', 200)}
        if response_type == 'json':
            return {'type': 'json', 'data': response.get('data'), 'status': response.get('status', 200)}
        if response_type == 'redirect':
            return {
                'type': 'redirect', 'url': response['url'], 'permanent': response.get('permanent', False),
            }
        if response_type == 'no_content':
            return {'type': 'no_content', 'status': response.get('status', 204)}
        raise TypeError(f'Unknown plugin route response type: {response_type!r}')

    @classmethod
    def _stream_final_payload(cls, response, plugin_id, config, request):
        return cls._render_payload(response, plugin_id, config, request)

    @classmethod
    def _response_from_payload(cls, response, plugin_id, config, request):
        rendered = cls._render_payload(response, plugin_id, config, request)
        response_type = rendered['type']
        if response_type == 'not_found':
            raise Http404
        if response_type == 'html':
            return HttpResponse(rendered['content'], status=rendered['status'])
        if response_type == 'json':
            http_response = JsonResponse(rendered['data'], status=rendered['status'], safe=False)
            for key, value in response.get('headers', {}).items():
                http_response[key] = value
            return http_response
        if response_type == 'redirect':
            response_class = (
                HttpResponsePermanentRedirect if rendered['permanent'] else HttpResponseRedirect
            )
            return response_class(rendered['url'])
        if response_type == 'no_content':
            http_response = HttpResponse(status=rendered['status'])
            for key, value in response.get('headers', {}).items():
                http_response[key] = value
            return http_response

    def get_service(self, user, plugin_id, name, args=None, request=None):
        """Call an enabled plugin service by its scoped name and return its result."""
        enabled_ids = self._enabled_ids(user, request)
        if plugin_id not in enabled_ids:
            return None
        worker = self.load(plugin_id, enabled_ids)
        if worker is None:
            return None
        if name not in worker.manifest['services']:
            return None
        try:
            result = self._call(worker, 'invoke_service', {'name': name, 'args': args or {}})
            return result.get('result')
        except PluginCallError:
            logger.exception('Service %s on plugin %s failed', name, plugin_id)
            return None


plugin_manager = PluginManager()
