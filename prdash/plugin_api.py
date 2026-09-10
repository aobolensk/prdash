"""Public interfaces for prdash plugins.

Plugin code runs in its own worker process (see docs/plugin-architecture.md)
and never receives live Django objects. Everything a hook, route, or UI
contribution receives is plain, JSON-serializable data.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence


PLUGIN_API_VERSION = '2.0'
GITHUB_API_VERSION = '2022-11-28'

PR_LIST_QUERY_HOOK = 'pr_list.query'
PR_LIST_PROCESS_HOOK = 'pr_list.process'
PR_LIST_FILTERS_SLOT = 'pr_list.filters'
HEADER_STATUS_SLOT = 'header.status'
HEAD_SLOT = 'head'
PR_CARD_ACTIONS_SLOT = 'pr_card.actions'


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
    context_provider: 'UIContextProvider | None' = None


@dataclass(frozen=True)
class NestedTemplate:
    """A sub-template pre-rendered and injected into an outer template's context."""

    template: TemplateResource
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginTemplateResponse:
    """A plugin route response rendered from a package resource.

    `nested` sub-templates are rendered first (each in its own context) and
    injected into `context` under their key, letting a route compose a page
    out of several packaged templates without the worker touching Django's
    template engine itself.
    """

    template: TemplateResource
    context: Mapping[str, Any] = field(default_factory=dict)
    status: int = 200
    nested: Mapping[str, NestedTemplate] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginJsonResponse:
    """A plugin route response serialized as a JSON body."""

    data: Any
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginRedirect:
    """A plugin route response that redirects the browser."""

    url: str
    permanent: bool = False


@dataclass(frozen=True)
class PluginNoContent:
    """A plugin route response with no body, e.g. for HTMX out-of-band triggers."""

    status: int = 204
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginStreamChunk:
    """Progress event a route generator may yield before returning its final response; kind/data are opaque to the host."""

    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestInfo:
    """A serializable snapshot of the parts of a request plugin code may use."""

    method: str
    path: str
    query_params: Mapping[str, str] = field(default_factory=dict)
    form_params: Mapping[str, str] = field(default_factory=dict)
    form_lists: Mapping[str, Sequence[str]] = field(default_factory=dict)
    body: str = ''
    headers: Mapping[str, str] = field(default_factory=dict)
    user_id: int | None = None
    username: str = ''
    is_authenticated: bool = False


@dataclass(frozen=True)
class PluginUserData:
    """A plugin-owned JSON value stored for one user in a named collection."""

    key: str
    value: Any
    created_at: Any
    updated_at: Any


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

    request: RequestInfo
    active_tab: str
    current_username: str | None
    current_repo: tuple[str, str] | None = None
    query_defaults: Mapping[str, str] = field(default_factory=dict)
    query: PullRequestQuery | None = None


Hook = Callable[[Any, Any, Mapping[str, Any]], Any]
# May also be a generator function yielding PluginStreamChunk values before returning its response.
Route = Callable[[RequestInfo, Mapping[str, Any]], Any]
UIContextProvider = Callable[[RequestInfo, Mapping[str, Any]], Mapping[str, Any]]


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

    def register_service(self, name: str, handler: Callable[[Mapping[str, Any]], Any]) -> None:
        ...

    def call_service(self, user_id: int, plugin_id: str, name: str, args: Mapping[str, Any]) -> Any:
        ...

    def resolve_github_token(self, user_id: int) -> str | None:
        ...

    def list_tracked_repositories(
        self, user_id: int, *, enabled_only: bool = False
    ) -> tuple[Mapping[str, Any], ...]:
        ...

    def get_username(self, user_id: int) -> str | None:
        ...

    def fetch_open_prs(
        self, user_id: int, repos: Sequence[tuple[str, str]], author: str | None = None
    ) -> tuple[Mapping[str, Any], ...]:
        ...

    def fetch_merged_prs(
        self, user_id: int, repos: Sequence[tuple[str, str]], author: str | None = None
    ) -> tuple[Mapping[str, Any], ...]:
        ...

    def fetch_reviews_for_stats(
        self, user_id: int, repos: Sequence[tuple[str, str]], username: str, days: int = 30
    ) -> Mapping[str, Any]:
        ...

    def get_user_config(self, user_id: int) -> Mapping[str, Any]:
        ...

    def update_user_config(self, user_id: int, values: Mapping[str, Any]) -> None:
        ...

    def list_user_data(self, user_id: int, collection: str) -> tuple[PluginUserData, ...]:
        ...

    def get_user_data(self, user_id: int, collection: str, key: str) -> PluginUserData | None:
        ...

    def set_user_data(self, user_id: int, collection: str, key: str, value: Any) -> PluginUserData:
        ...

    def delete_user_data(self, user_id: int, collection: str, key: str) -> bool:
        ...

    def reorder_user_data(self, user_id: int, collection: str, keys: Sequence[str]) -> None:
        ...


class Plugin(Protocol):
    """The object exposed through a prdash plugin entry point."""

    metadata: PluginMetadata

    def initialize(self, registrar: PluginRegistrar) -> None:
        ...

    def shutdown(self) -> None:
        ...
