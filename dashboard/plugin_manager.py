"""Discovery and runtime isolation for prdash plugins."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import import_module, metadata, resources
import json
import logging
from pathlib import Path
import re
import sys
from threading import RLock
from typing import Any

from django.conf import settings
from django.db import OperationalError, ProgrammingError, transaction
from django.http import Http404, HttpResponse, HttpResponseServerError
from django.template import engines
from django.utils.safestring import mark_safe
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from prdash.plugin_api import (
    PLUGIN_API_VERSION,
    PluginMetadata,
    PluginTemplateResponse,
    TemplateResource,
    UIContribution,
)

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = 'prdash.plugins'
SOURCE_MANIFEST = 'prdash-plugin.json'
IDENTIFIER_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


class PluginActivationError(RuntimeError):
    """A user-specific activation problem, such as a disabled dependency."""


@dataclass
class PluginDescriptor:
    """Metadata available without importing plugin implementation code."""

    plugin_id: str
    name: str
    version: str
    description: str
    entrypoint: str
    source: str
    api_version: str | None = None
    python_path: Path | None = None
    entry_point: Any = None
    load_error: str | None = None


@dataclass
class _Registration:
    hooks: list[tuple[str, int, Any]] = field(default_factory=list)
    ui: list[UIContribution] = field(default_factory=list)
    routes: dict[str, Any] = field(default_factory=dict)
    services: dict[str, Any] = field(default_factory=dict)


@dataclass
class _LoadedPlugin:
    descriptor: PluginDescriptor
    plugin: Any
    registration: _Registration


class _Registrar:
    def __init__(self, manager, plugin_id, registration):
        self._manager = manager
        self._plugin_id = plugin_id
        self._registration = registration

    @property
    def deployment_config(self):
        plugin_config = getattr(settings, 'PRDASH_PLUGIN_CONFIG', {})
        return dict(plugin_config.get(self._plugin_id, {}))

    def register_hook(self, name, callback, *, priority=100):
        if not name or not callable(callback):
            raise ValueError('Plugin hooks require a name and callable')
        self._registration.hooks.append((name, priority, callback))

    def register_ui(self, contribution):
        if not isinstance(contribution, UIContribution):
            raise TypeError('UI contributions must use UIContribution')
        self._registration.ui.append(contribution)

    def register_route(self, name, callback):
        if not IDENTIFIER_PATTERN.fullmatch(name) or not callable(callback):
            raise ValueError('Plugin routes require a simple name and callable')
        if name in self._registration.routes:
            raise ValueError(f'Duplicate plugin route: {name}')
        self._registration.routes[name] = callback

    def register_service(self, name, service):
        if not name:
            raise ValueError('Plugin services require a name')
        if name in self._registration.services:
            raise ValueError(f'Duplicate plugin service: {name}')
        self._registration.services[name] = service

    def get_user_config(self, user):
        return self._manager.get_user_config(user, self._plugin_id)

    def update_user_config(self, user, values):
        self._manager.update_user_config(user, self._plugin_id, values)


class PluginManager:
    """Process-local registry backed by per-user activation state."""

    def __init__(self):
        self.descriptors: dict[str, PluginDescriptor] = {}
        self.discovery_errors: list[str] = []
        self._loaded: dict[str, _LoadedPlugin] = {}
        self._loading: set[str] = set()
        self._activation_errors: dict[tuple[frozenset[str], str], str] = {}
        self._lock = RLock()

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
                entry_point=entry_point,
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

    @contextmanager
    def _source_import_path(self, descriptor):
        if descriptor.python_path is None:
            yield
            return

        path = str(descriptor.python_path)
        sys.path.insert(0, path)
        try:
            yield
        finally:
            try:
                sys.path.remove(path)
            except ValueError:
                pass

    @staticmethod
    def _load_object(target):
        module_name, separator, attribute = target.partition(':')
        if not separator or not module_name or not attribute:
            raise ValueError(f'Invalid plugin entry point: {target}')
        return getattr(import_module(module_name), attribute)

    @staticmethod
    def _materialize(candidate):
        if isinstance(candidate, type):
            return candidate()
        if callable(candidate) and not hasattr(candidate, 'initialize'):
            return candidate()
        return candidate

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

            loaded = self._loaded.get(plugin_id)
            if loaded is not None:
                try:
                    self._validate_dependencies(
                        loaded.plugin.metadata,
                        enabled_plugin_ids,
                    )
                    for dependency in loaded.plugin.metadata.dependencies:
                        if self.load(dependency.plugin_id, enabled_plugin_ids) is None:
                            raise PluginActivationError(
                                f'Dependency {dependency.plugin_id!r} could not be loaded'
                            )
                    return loaded
                except PluginActivationError as error:
                    self._activation_errors[activation_key] = str(error)
                    return None

            if plugin_id in self._loading:
                self._activation_errors[activation_key] = (
                    f'Dependency cycle includes {plugin_id!r}'
                )
                return None

            plugin = None
            initialization_started = False
            self._loading.add(plugin_id)
            try:
                with self._source_import_path(descriptor):
                    candidate = (
                        descriptor.entry_point.load()
                        if descriptor.entry_point is not None
                        else self._load_object(descriptor.entrypoint)
                    )
                plugin = self._materialize(candidate)
                plugin_metadata = plugin.metadata
                self._validate_metadata(descriptor, plugin_metadata)
                descriptor.name = plugin_metadata.name
                descriptor.description = plugin_metadata.description
                descriptor.api_version = plugin_metadata.api_version
                self._validate_dependencies(plugin_metadata, enabled_plugin_ids)

                for dependency in plugin_metadata.dependencies:
                    dependency_plugin = self.load(dependency.plugin_id, enabled_plugin_ids)
                    if dependency_plugin is None:
                        raise PluginActivationError(
                            f'Dependency {dependency.plugin_id!r} could not be loaded'
                        )

                registration = _Registration()
                registrar = _Registrar(self, plugin_id, registration)
                initialization_started = True
                plugin.initialize(registrar)
                loaded = _LoadedPlugin(descriptor, plugin, registration)
                self._loaded[plugin_id] = loaded
                return loaded
            except PluginActivationError as error:
                self._activation_errors[activation_key] = str(error)
                return None
            except Exception as error:
                descriptor.load_error = str(error) or error.__class__.__name__
                logger.exception('Failed to load plugin %s', plugin_id)
                return None
            finally:
                self._loading.discard(plugin_id)
                if initialization_started and plugin_id not in self._loaded:
                    try:
                        plugin.shutdown()
                    except Exception:
                        logger.exception(
                            'Failed to clean up plugin %s after initialization error',
                            plugin_id,
                        )

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
        """Shut down a loaded plugin and remove all process-local registrations."""
        with self._lock:
            loaded = self._loaded.pop(plugin_id, None)
            if loaded is None:
                return
            try:
                loaded.plugin.shutdown()
            except Exception:
                logger.exception('Failed to shut down plugin %s', plugin_id)

    def _shutdown_all(self):
        for plugin_id in list(self._loaded):
            self.unload(plugin_id)

    @staticmethod
    def _state_model():
        from .models import PluginConfiguration
        return PluginConfiguration

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

    def configure_user(self, user, enabled_plugin_ids):
        """Persist the explicit enabled set and update this process registry."""
        selected = set(enabled_plugin_ids) & self.descriptors.keys()
        state_model = self._state_model()
        with transaction.atomic():
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

        for plugin_id in set(self._loaded) - selected:
            if not state_model.objects.filter(plugin_id=plugin_id, enabled=True).exists():
                self.unload(plugin_id)

    def plugin_statuses(self, user, request=None):
        states = self._state_map(user, request)
        enabled_ids = self._enabled_ids(user, request)
        for plugin_id in enabled_ids:
            self.load(plugin_id, enabled_ids)

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
                'loaded': descriptor.plugin_id in self._loaded,
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

    def run_hook(self, name, value, hook_context, user, request=None):
        """Run enabled hook callbacks in order and isolate individual failures."""
        states = self._state_map(user, request)
        enabled_ids = self._enabled_ids(user, request)
        active_plugin_ids = set()
        for plugin_id in enabled_ids:
            if self.load(plugin_id, enabled_ids) is not None:
                active_plugin_ids.add(plugin_id)

        callbacks = []
        for plugin_id in active_plugin_ids:
            loaded = self._loaded.get(plugin_id)
            config = dict(states[plugin_id].config)
            for index, (hook_name, priority, callback) in enumerate(
                loaded.registration.hooks
            ):
                if hook_name == name:
                    callbacks.append((priority, plugin_id, index, callback, config))

        for _, plugin_id, _, callback, config in sorted(
            callbacks,
            key=lambda item: item[:3],
        ):
            try:
                result = callback(value, hook_context, config)
                if result is not None:
                    value = result
            except Exception:
                logger.exception('Plugin %s failed in hook %s', plugin_id, name)
        return value

    def render_slot(self, slot, template_context, **extra_context):
        """Render enabled UI contributions for one template slot."""
        request = template_context.get('request')
        user = getattr(request, 'user', None)
        states = self._state_map(user, request)
        enabled_ids = self._enabled_ids(user, request)
        active_plugin_ids = set()
        for plugin_id in enabled_ids:
            if self.load(plugin_id, enabled_ids) is not None:
                active_plugin_ids.add(plugin_id)

        contributions = []
        for plugin_id in active_plugin_ids:
            loaded = self._loaded.get(plugin_id)
            for index, contribution in enumerate(loaded.registration.ui):
                if contribution.slot == slot:
                    contributions.append((
                        contribution.order,
                        plugin_id,
                        index,
                        contribution,
                    ))

        base_context = template_context.flatten()
        base_context.update(extra_context)
        rendered = []
        for _, plugin_id, _, contribution in sorted(
            contributions,
            key=lambda item: item[:3],
        ):
            context = dict(base_context)
            context['plugin_id'] = plugin_id
            context['plugin_config'] = dict(states[plugin_id].config)
            try:
                rendered.append(self._render_resource(
                    contribution.template,
                    context,
                    request,
                ))
            except Exception:
                logger.exception('Plugin %s failed to render slot %s', plugin_id, slot)
        return mark_safe(''.join(rendered))

    @staticmethod
    def _render_resource(template_resource, context, request):
        if not isinstance(template_resource, TemplateResource):
            raise TypeError('Plugin templates must use TemplateResource')
        source = resources.files(template_resource.package).joinpath(
            template_resource.path
        ).read_text(encoding='utf-8')
        template = engines['django'].from_string(source)
        return template.render(context, request)

    def dispatch(self, request, plugin_id, route):
        """Dispatch a request to an enabled plugin route."""
        enabled_ids = self._enabled_ids(request.user, request)
        if plugin_id not in enabled_ids:
            raise Http404
        loaded = self.load(plugin_id, enabled_ids)
        if loaded is None:
            return HttpResponseServerError('')
        callback = loaded.registration.routes.get(route)
        if callback is None:
            raise Http404
        config = dict(self._state_map(request.user, request)[plugin_id].config)
        try:
            response = callback(request, config)
            if isinstance(response, PluginTemplateResponse):
                content = self._render_resource(response.template, response.context, request)
                return HttpResponse(content, status=response.status)
            if isinstance(response, HttpResponse):
                return response
            raise TypeError('Plugin routes must return HttpResponse or PluginTemplateResponse')
        except Http404:
            raise
        except Exception:
            logger.exception('Plugin %s failed in route %s', plugin_id, route)
            return HttpResponseServerError('')

    def get_service(self, user, plugin_id, name, request=None):
        """Return an enabled plugin service by its scoped name."""
        enabled_ids = self._enabled_ids(user, request)
        if plugin_id not in enabled_ids:
            return None
        loaded = self.load(plugin_id, enabled_ids)
        if loaded is None:
            return None
        return loaded.registration.services.get(name)


plugin_manager = PluginManager()
