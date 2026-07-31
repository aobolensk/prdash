"""Public interfaces for prdash plugins."""

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol


PLUGIN_API_VERSION = '1.0'

PR_LIST_QUERY_HOOK = 'pr_list.query'
PR_LIST_PROCESS_HOOK = 'pr_list.process'
PR_LIST_FILTERS_SLOT = 'pr_list.filters'
HEADER_STATUS_SLOT = 'header.status'
HEAD_SLOT = 'head'
SETTINGS_SLOT = 'settings'


@dataclass(frozen=True)
class PluginDependency:
    """A dependency on another enabled prdash plugin."""

    plugin_id: str
    version: str = ''


@dataclass(frozen=True)
class PluginMetadata:
    """Identity and compatibility information supplied by a plugin."""

    plugin_id: str
    name: str
    version: str
    api_version: str
    description: str = ''
    dependencies: tuple[PluginDependency, ...] = ()


@dataclass(frozen=True)
class TemplateResource:
    """A UTF-8 Django template stored in an importable Python package."""

    package: str
    path: str


@dataclass(frozen=True)
class UIContribution:
    """A template rendered in a named core UI slot."""

    slot: str
    template: TemplateResource
    order: int = 100


@dataclass(frozen=True)
class PluginTemplateResponse:
    """A plugin route response rendered from a package resource."""

    template: TemplateResource
    context: Mapping[str, Any] = field(default_factory=dict)
    status: int = 200


@dataclass
class PullRequestQuery:
    """Plugin-controlled query state for a pull request list request."""

    parameters: dict[str, str] = field(default_factory=dict)
    fetch_options: dict[str, Any] = field(default_factory=dict)
    cache_vary: dict[str, str] = field(default_factory=dict)
    affects_count: bool = False


@dataclass
class PullRequestListContext:
    """Stable context passed to pull request list hooks."""

    request: Any
    client: Any
    active_tab: str
    current_username: str | None
    current_repo: tuple[str, str] | None = None
    query_defaults: Mapping[str, str] = field(default_factory=dict)
    query: PullRequestQuery | None = None


Hook = Callable[[Any, Any, Mapping[str, Any]], Any]
Route = Callable[[Any, Mapping[str, Any]], Any]


class PluginRegistrar(Protocol):
    """Capabilities available while a plugin initializes."""

    @property
    def deployment_config(self) -> Mapping[str, Any]:
        ...

    def register_hook(self, name: str, callback: Hook, *, priority: int = 100) -> None:
        ...

    def register_ui(self, contribution: UIContribution) -> None:
        ...

    def register_route(self, name: str, callback: Route) -> None:
        ...

    def register_service(self, name: str, service: Any) -> None:
        ...

    def get_user_config(self, user: Any) -> Mapping[str, Any]:
        ...

    def update_user_config(self, user: Any, values: Mapping[str, Any]) -> None:
        ...


class Plugin(Protocol):
    """The object exposed through a prdash plugin entry point."""

    metadata: PluginMetadata

    def initialize(self, registrar: PluginRegistrar) -> None:
        ...

    def shutdown(self) -> None:
        ...
