# Plugin development

A plugin is a Python object with metadata, `initialize`, and `shutdown`, loaded
inside its own worker subprocess from a local source manifest or an installed
wheel entry point. Set `PluginMetadata.api_version` to `2.0`. For source plugins,
set the manifest `api_version` to the same value.

## Minimal plugin

```python
from prdash.plugin_api import (
    PLUGIN_API_VERSION,
    PluginJsonResponse,
    PluginMetadata,
    RequestInfo,
)


class ExamplePlugin:
    metadata = PluginMetadata(
        plugin_id='example',
        name='Example',
        version='1.0.0',
        api_version=PLUGIN_API_VERSION,
        description='Small example plugin.',
    )

    def initialize(self, registrar):
        self.registrar = registrar
        registrar.register_route('hello', self.hello)
        registrar.register_service('message', self.message)

    def shutdown(self):
        pass

    def hello(self, request: RequestInfo, config):
        if request.user_id is None:
            return PluginJsonResponse({'error': 'Authentication required'}, status=401)
        return PluginJsonResponse({'message': 'hello', 'username': request.username})

    @staticmethod
    def message(args):
        return {'message': 'hello'}


plugin = ExamplePlugin()
```

Route callbacks receive `(request, config)`, where `request` is a `RequestInfo`
snapshot and `config` is the plugin configuration for the current user. They do
not receive Django request or response objects. Return a plugin API response such as
`PluginJsonResponse`, `PluginTemplateResponse`, `PluginRedirect`, or
`PluginNoContent`. Service handlers receive their JSON-compatible argument
mapping. Hook callbacks receive `(value, context, config)` and return the next
value. A hook failure leaves the previous value in place.
For pull request list hooks, `context` is a `PullRequestListContext`. Its
`request` field is also a `RequestInfo`; the other fields describe the active
tab, username, repository, query defaults, and current query.

`RequestInfo` contains:

| Field | Contents |
| --- | --- |
| `method`, `path` | HTTP method and path. |
| `query_params` | Query parameter values as strings. Repeated keys are collapsed to the last value. |
| `form_params` | POST form values as strings, with repeated keys collapsed to the last value. |
| `form_lists` | POST form values as lists, preserving repeated keys. |
| `body` | Decoded request body text for POST, PUT, or PATCH requests, otherwise an empty string. Use this to parse JSON request bodies. |
| `headers` | Only `HX-Request`, `HX-Target`, `HX-Trigger`, `Content-Type`, `Accept`, and `Sec-Fetch-Mode`. |
| `user_id`, `username`, `is_authenticated` | Basic user identity. `user_id` is `None` for unauthenticated requests. |

Cookies, sessions, arbitrary headers, and the live Django user object are not
included. Check `request.user_id` before calling registrar methods that take a
user id.

## Source layout

```text
my-plugin/
  prdash-plugin.json
  pyproject.toml
  src/
    my_prdash_plugin/
      __init__.py
      plugin.py
      templates/
```

`prdash-plugin.json`:

```json
{
  "id": "example",
  "name": "Example",
  "version": "1.0.0",
  "api_version": "2.0",
  "description": "Small example plugin.",
  "entrypoint": "my_prdash_plugin.plugin:plugin",
  "python_path": "src"
}
```

Add the directory containing `my-plugin/` to `PRDASH_PLUGIN_PATHS`. Restart Django,
then enable Example under **Settings > Plugins**. Discovery reads only the JSON
manifest. The module is imported after activation.

## Wheel packaging

Add the matching entry point to `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "my-prdash-plugin"
version = "1.0.0"
requires-python = ">=3.12"

[project.entry-points."prdash.plugins"]
example = "my_prdash_plugin.plugin:plugin"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
my_prdash_plugin = ["templates/*.html"]
```

Build and inspect the wheel:

```bash
python -m pip wheel --no-deps --wheel-dir dist .
unzip -l dist/my_prdash_plugin-1.0.0-py3-none-any.whl
```

Install the wheel into the prdash environment and restart Django. No source code
needs to be added to the main repository. Private package indexes, private wheel
files, and source-obscuring binary extensions are distribution choices outside
the framework.

Editable installs use the same wheel entry point:

```bash
python -m pip install -e /path/to/my-plugin
```

If a source manifest and an installed entry point have the same plugin id, the
configured source manifest takes precedence.

## UI resources

Package templates and register them by resource name:

```python
from prdash.plugin_api import TemplateResource, UIContribution

registrar.register_ui(UIContribution(
    slot='header.status',
    template=TemplateResource(
        package='my_prdash_plugin',
        path='templates/header.html',
    ),
))
```

Templates are rendered by the host and receive the normal Django template
context plus `plugin_id` and `plugin_config`. The optional `context_provider`
callback runs in the worker and receives `(request: RequestInfo, config)`. Public
slots are listed in `prdash.plugin_api` and the dashboard templates.

The core fuzzy search exposes a browser API after the
`prdash:pullRequestSearchReady` event:

```javascript
document.addEventListener('prdash:pullRequestSearchReady', function() {
    const search = window.prdash.pullRequestSearch;
    const state = search.getState();
    search.setState({
        open: true,
        include: {text: 'bug', pills: [{kind: 'label', value: 'urgent'}]},
        exclude: {text: '', pills: []}
    });
});
```

`getState()` returns this same JSON-serializable structure. `setState()`
returns `false` when the current page has no PR search UI. Plugins should store
this state unchanged and validate it again in their route handlers.

For request-specific UI data, pass a context provider when registering a
contribution. It receives `(request, config)` in the worker and returns a
mapping merged into the template context by the host:

```python
registrar.register_ui(UIContribution(
    slot='pr_list.filters',
    template=TemplateResource(package='my_prdash_plugin', path='templates/filters.html'),
    context_provider=lambda request, config: {'items': []},
))
```

## Dependencies and configuration

Declare Python dependencies under `[project].dependencies`. Declare another
plugin dependency in metadata:

```python
from prdash.plugin_api import PluginDependency

dependencies=(PluginDependency('other-plugin', '>=1.2,<2'),)
```

The user must enable both plugins. prdash does not activate dependencies
implicitly.

Deployment configuration is read once during initialization:

```python
self.timeout = registrar.deployment_config.get('timeout', 5)
```

Per-user configuration remains plugin-owned. Registrar methods take the integer
user id from `RequestInfo`, not a Django user object. Merge values before saving
because `update_user_config` replaces the stored mapping:

```python
user_id = request.user_id
if user_id is not None:
    config = registrar.get_user_config(user_id)
    registrar.update_user_config(user_id, {**config, 'timeout': 10})
```

Validate user values before saving them. Never place secrets in template context
or logs.

Use named user-data collections for user-created records rather than packing a
growing list into plugin configuration:

```python
user_id = request.user_id
if user_id is not None:
    registrar.set_user_data(user_id, 'saved_views', 'daily', {'query': {'author': 'octocat'}})
    saved_views = registrar.list_user_data(user_id, 'saved_views')
```

Collection names are simple identifiers and values must be JSON serializable.
Each key is unique for a user, plugin, and collection.

Collections can also preserve a user-selected order. Pass every current key in
its desired order:

```python
user_id = request.user_id
if user_id is not None:
    registrar.reorder_user_data(user_id, 'saved_views', ('daily', 'weekly'))
```
