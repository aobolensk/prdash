# Plugin architecture

prdash has a small, in-process plugin API. Discovery is separate from loading:
installed code is visible in Settings, but it is not imported or executed until a
user explicitly enables it.

## Components

- `prdash.plugin_api` is the public, versioned author interface.
- `dashboard.plugin_manager` owns discovery, registrations, isolation, and dispatch.
- `PluginConfiguration` stores per-user activation and plugin-owned JSON settings.
- `PluginUserData` stores plugin-owned, named collections of per-user JSON values.
- `plugins/` contains the reference implementations. It is not core code.

The current API version is `1.1`. A plugin API with the same major version is
compatible. A future incompatible contract will use a new major version.

## Lifecycle

| Phase | Behavior |
| --- | --- |
| Discovery | Every Django process scans configured source manifests and the `prdash.plugins` entry-point group during startup. Plugin implementation modules are not imported. |
| Loading | The first request for a previously enabled user, or the Settings save that enables a plugin, imports the entry point. |
| Validation | prdash checks the plugin id, API major version, installed dependencies, dependency versions, and explicit activation of plugin dependencies. |
| Initialization | `initialize(registrar)` registers hooks, UI contributions, routes, and services in a plugin-scoped registry. |
| Execution | Only plugins enabled for the current user participate. Hook order is priority first and plugin id second. |
| Shutdown | When the last enabled user disables a plugin in a process, `shutdown()` runs and registrations are discarded. Process restart also starts with a clean registry. |
| Unloading | Python does not safely remove imported modules. prdash unloads capabilities and references, while module objects may remain in `sys.modules` until process exit. |

If a plugin is removed from source paths or uninstalled, the next Django restart no
longer discovers or loads it. A stale user configuration is ignored.

## Capabilities

Plugins register through the scoped registrar passed to `initialize`:

- Hooks transform a value at a documented hook such as `pr_list.query` or
  `pr_list.process`.
- UI contributions render packaged Django templates in documented slots such as
  `head`, `header.status`, `pr_list.filters`, `pr_card.actions`, or `settings`. A contribution can
  provide request-specific template context without adding a core view contract.
- Routes are reached through the core dispatcher at
  `/plugins/<plugin-id>/<route>/`; a disabled plugin route returns 404.
- Services expose plugin-owned objects under names scoped by plugin id.

Commands are deliberately not a v1 runtime capability. Lazy, per-user activation
does not fit Django command discovery through `INSTALLED_APPS`. A plugin that needs
a standalone command can publish a standard Python console script. A generic
plugin-command dispatcher can be added later without changing the current
interfaces.

## Activation and configuration

Activation is per user and defaults to disabled. The enabled set is stored in the
database and therefore gates execution consistently across Django workers. A
worker loads enabled code lazily on the next relevant request.

There are two configuration scopes:

- Deployment values live in `PRDASH_PLUGIN_CONFIG` and are exposed as
  `registrar.deployment_config`.
- Per-user values live in `PluginConfiguration.config`. A plugin reads and
  validates them through `registrar.get_user_config()` and
  `registrar.update_user_config()`. A plugin can contribute its own form to the
  `settings` slot and handle it through a registered route.
- User-owned collections live in `PluginUserData`. A plugin uses
  `list_user_data`, `get_user_data`, `set_user_data`, `delete_user_data`, and
  `reorder_user_data` with a plugin-defined collection name and key. The core
  stores opaque JSON and does not impose a schema, so this supports saved views,
  rules, bookmarks, and similar user-created plugin data.

The framework intentionally does not interpret arbitrary plugin schemas.

## Dependencies and compatibility

Python library dependencies belong in wheel metadata and are resolved by `pip`.
Dependencies on other plugins use `PluginDependency(plugin_id, version_specifier)`.
The dependency must be installed and explicitly enabled for the same user. Version
specifiers use the standard Python packaging syntax.

Source manifests expose compatibility metadata without importing implementation
code. Wheel plugins are checked again when their entry point is loaded.

## Failure boundaries

Discovery, loading, initialization, shutdown, each hook call, each UI contribution,
and each route dispatch have separate exception boundaries. A failure is logged,
the broken contribution is skipped, and the main request continues where a safe
fallback exists. Route failures return an empty 500 response. Load failures appear
in Settings.

These boundaries protect availability, not confidentiality. In-process plugins
have the same filesystem, database, network, and Python access as prdash.

## In-process versus out-of-process

The initial implementation is in-process because it has low latency, no
serialization cost, and direct Django template and request integration. It is
appropriate for trusted open-source or closed-source packages.

Out-of-process execution would isolate crashes and restrict privileges, but it
would require worker supervision, RPC contracts, authentication, serialization,
timeouts, and narrower UI APIs. It should be a separate execution backend if
untrusted plugins become a requirement.

## Testing

Core tests cover discovery without import, explicit activation, compatibility
rejection, hook and route isolation, service lookup, shutdown, and settings
integration. Each plugin should additionally test:

1. Metadata and registration.
2. Every hook with plain contract objects.
3. Packaged templates and routes through Django integration tests.
4. Both source-manifest loading and a built wheel containing entry-point metadata
   and template resources.

## Initial migration

The extraction was performed in these increments:

1. Add the public API, discovery registry, user activation model, generic route,
   and UI slots.
2. Replace core filter parsing and sorting with the `pr_list.query` and
   `pr_list.process` hooks.
3. Move filter HTML and behavior into `prdash-pull-request-filters`.
4. Replace the fixed header endpoint with a plugin UI slot and route.
5. Move status polling, templates, and styles into `prdash-github-status`.
6. Remove the core `show_github_status` field. Both plugins now start disabled so
   existing users must make the requested explicit activation choice.
7. Move the Stats page, its HTMX partials, and `StatsService` into `prdash-stats`,
   contributing its header nav icon through the existing `header.status` slot.

The old reviewed and approved review-request URLs remain as redirects to the
filter query form.
