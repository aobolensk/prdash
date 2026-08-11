# Plugin development

A plugin is a Python object with metadata, `initialize`, and `shutdown`. The same
object can be loaded from a local source manifest or an installed wheel entry
point.

## Minimal plugin

```python
from django.http import HttpResponse

from prdash.plugin_api import PLUGIN_API_VERSION, PluginMetadata


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
        registrar.register_service('message', 'hello')

    def shutdown(self):
        pass

    def hello(self, request, config):
        return HttpResponse('hello')


plugin = ExamplePlugin()
```

Route callbacks receive the Django request and the current user configuration.
Hook callbacks receive `(value, context, config)` and return the next value. A hook
failure leaves the previous value in place.

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
  "api_version": "1.0",
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

Templates receive the normal Django request context plus `plugin_id` and
`plugin_config`. Public v1 slots are listed in `prdash.plugin_api`.

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
contribution. It receives `(request, config)` and returns a mapping merged into
the template context:

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

Per-user configuration remains plugin-owned:

```python
config = registrar.get_user_config(request.user)
registrar.update_user_config(request.user, {'timeout': 10})
```

Validate user values before saving them. Never place secrets in template context
or logs.

Use named user-data collections for user-created records rather than packing a
growing list into plugin configuration:

```python
registrar.set_user_data(request.user, 'saved_views', 'daily', {'query': {'author': 'octocat'}})
saved_views = registrar.list_user_data(request.user, 'saved_views')
```

Collection names are simple identifiers and values must be JSON serializable.
Each key is unique for a user, plugin, and collection.

Collections can also preserve a user-selected order. Pass every current key in
its desired order:

```python
registrar.reorder_user_data(request.user, 'saved_views', ('daily', 'weekly'))
```
