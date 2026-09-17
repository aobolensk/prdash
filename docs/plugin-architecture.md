# Plugin architecture

prdash has a subprocess-based plugin API. Discovery is separate from loading:
installed code is visible in Settings, but it is not imported or executed until a
user explicitly enables it. The Django host communicates with each plugin worker
over a JSON protocol on standard input and output.

## Components

- `prdash.plugin_api` is the public, versioned author interface.
- `dashboard.plugin_manager` owns discovery, registrations, isolation, and dispatch.
- `PluginConfiguration` stores per-user activation and plugin-owned JSON settings.
- `PluginUserData` stores plugin-owned, named collections of per-user JSON values.
- `plugins/` contains the reference implementations. It is not core code.

The current API version is `2.0`. A plugin API with the same major version is
compatible. A future incompatible contract will use a new major version.

## Lifecycle

| Phase | Behavior |
| --- | --- |
| Discovery | Every Django process scans configured source manifests and the `prdash.plugins` entry-point group during startup. Plugin implementation modules are not imported. |
| Loading | The first request that activates an enabled plugin, or the Settings save that enables it, starts a worker subprocess and imports the entry point there. |
| Validation | prdash checks plugin identity and version against discovered metadata, API major version, installed dependencies, dependency versions, and explicit activation of plugin dependencies. |
| Initialization | The worker calls `initialize(registrar)`, then reports its metadata, hooks, UI contributions, routes, and services to the host. |
| Execution | Only plugins enabled for the current user participate. Calls and results cross the worker protocol as JSON. Hook order is priority first and plugin id second. |
| Shutdown | When a Settings save leaves no users with a plugin enabled, the handling Django process calls `shutdown()` and terminates its worker. A process restart also ends its workers. |
| Unloading | The worker process exits, so its imported plugin modules and registrations are discarded. |

If a plugin is removed from source paths or uninstalled, the next Django restart no
longer discovers or loads it. A stale user configuration is ignored.

## Capabilities

Plugins register through the scoped registrar passed to `initialize`:

- Hooks transform a value at a documented hook such as `pr_list.query` or
  `pr_list.process`.
- UI contributions render packaged Django templates in documented slots such as
  `head`, `header.status`, `pr_list.filters`, `pr_card.actions`, `pr_card.meta`,
  `pr_card.badges`, or `settings`. A contribution can provide request-specific
  template context without adding a core view contract.
- Routes are reached through the core dispatcher at
  `/plugins/<plugin-id>/<route>/`; a disabled plugin route returns 404.
- Services expose plugin-owned operations under names scoped by plugin id.
  Arguments and results cross the worker boundary as JSON-compatible values.

Commands are not a plugin runtime capability. Lazy, per-user activation
does not fit Django command discovery through `INSTALLED_APPS`. A plugin that needs
a standalone command can publish a standard Python console script. A generic
plugin-command dispatcher can be added later without changing the current
interfaces.

## Activation and configuration

Activation is per user and defaults to disabled. The enabled set is stored in the
database and therefore gates execution consistently across Django workers. A
worker starts when Settings enables a plugin or a request first needs it.

There are two configuration scopes:

- Deployment values live in `PRDASH_PLUGIN_CONFIG` and are exposed as
  `registrar.deployment_config`.
- Per-user values live in `PluginConfiguration.config`. A plugin reads and
  validates them through `registrar.get_user_config(user_id)` and
  `registrar.update_user_config(user_id, values)`. The user id comes from the
  request snapshot. A plugin can contribute its own form to the `settings` slot
  and handle it through a registered route.
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

These boundaries protect availability, not confidentiality. The host does not
pass live Django objects through the plugin API. Plugin code may still import
packages available to the worker, and subprocesses run with the host operating
system account permissions. The worker boundary is not an operating system
sandbox.

## Worker process and callbacks

Each enabled plugin has a worker process managed by the Django host. Plugin code
uses the versioned `prdash.plugin_api` objects. The host sends calls to the worker
and services registrar operations, such as reading plugin configuration,
accessing plugin-owned user data, looking up tracked repositories, or fetching
GitHub data. These operations return JSON-compatible values rather than Django
model instances.

Routes and UI context providers receive a `RequestInfo` snapshot with the HTTP
method, path, query and form values, selected headers, body text, and basic user
identity. They do not receive the Django request, session, cookies, or response
objects. Route handlers return API response objects such as
`PluginJsonResponse` or `PluginTemplateResponse`. UI templates are packaged with
the plugin and rendered by the host Django process.

## Testing

Core tests cover discovery without import, explicit activation, compatibility
rejection, worker dispatch, hook and route isolation, service lookup, shutdown,
and settings integration. Each plugin should additionally test:

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
