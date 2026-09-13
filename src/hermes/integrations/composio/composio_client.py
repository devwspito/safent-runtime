"""Composio SDK client — wraps the synchronous composio SDK in asyncio.to_thread.

The REST v1/v2 API was retired (HTTP 410). This module uses the official
`composio` SDK (0.13.1) as the sole transport layer.

All SDK calls are synchronous; they are run off the event loop via a single
`_guarded` choke-point which also maps SDK exceptions to `ComposioApiError`.

Tool catalog is cached per toolkit_slug for 1 hour to avoid hammering the
API on every run_cycle invocation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from composio import Composio
from composio.exceptions import ComposioError
from composio_client import APIError, APIStatusError

logger = logging.getLogger(__name__)

_CACHE_TTL = 3600  # 1 hour, mirrors agents-autonomy/tool_catalog.py
_SAFE_DETAIL_MAX = 300

# Integraciones "OAuth simple": se conectan con un solo enlace de navegador
# (managed OAuth). Las demás (API_KEY/BASIC/BEARER) exigen credenciales/campos
# y NO se soportan todavía. Esta clasificación es COMPARTIDA: la usan el SO, la
# TUI y la tool del agente (connect_integration) — la fuente única es el daemon.
_OAUTH_SIMPLE_SCHEMES = frozenset({"OAUTH2", "OAUTH1"})
ADS_TOOLKITS = frozenset({"googleads", "metaads"})


def _managed_oauth_available(item: Any) -> bool | None:
    """SDK 0.13.1 explicitly distinguishes supported auth from managed auth."""
    schemes = getattr(item, "composio_managed_auth_schemes", None)
    if not isinstance(schemes, (list, tuple)):
        return None
    return bool({str(value).upper() for value in schemes} & _OAUTH_SIMPLE_SCHEMES)


def _extract_auth_schemes(item: Any) -> tuple[str, ...]:
    """Best-effort: lee los auth schemes de un toolkit del SDK (defensivo).

    El SDK de Composio expone el esquema de auth con nombres variables según
    versión. Probamos varias rutas; si no se encuentra, devolvemos () (=desconocido,
    fail-open: no bloqueamos lo que no podemos clasificar).
    """
    candidates: list[Any] = []
    for attr in ("auth_schemes", "authScheme", "auth_scheme"):
        val = getattr(item, attr, None)
        if val:
            candidates = val if isinstance(val, (list, tuple)) else [val]
            break
    if not candidates:
        meta = getattr(item, "meta", None)
        for attr in ("auth_schemes", "auth_config_details", "categories"):
            val = getattr(meta, attr, None) if meta is not None else None
            if val and attr == "auth_schemes":
                candidates = val if isinstance(val, (list, tuple)) else [val]
                break
    out: list[str] = []
    for c in candidates:
        mode = (
            getattr(c, "mode", None) or getattr(c, "auth_mode", None)
            or getattr(c, "scheme", None) or c
        )
        if isinstance(mode, str):
            out.append(mode.strip().upper())
    return tuple(out)


def _is_oauth_simple(schemes: tuple[str, ...]) -> bool:
    """OAuth simple si HAY algún esquema OAuth, o si no se pudo determinar (fail-open)."""
    if not schemes:
        return True  # desconocido → no bloquear (la conexión gestionada decidirá)
    return bool(set(schemes) & _OAUTH_SIMPLE_SCHEMES)

# Module-level tool cache keyed by toolkit_slug.
# Evicted lazily when TTL expires.
_tool_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}
_cache_lock: asyncio.Lock | None = None

_T = TypeVar("_T")


def _get_cache_lock() -> asyncio.Lock:
    # Module-level singleton: asyncio.Lock cannot be created at import time
    # (requires a running event loop in Python <3.10).  Initialise lazily.
    global _cache_lock  # noqa: PLW0603
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def _safe_detail(exc: APIStatusError) -> str:
    """Extract a safe, truncated error detail from an APIStatusError.

    Never re-echo the API key (it won't be present in the body, but we
    truncate defensively).
    """
    raw: str = ""
    if exc.body is not None:
        raw = str(exc.body)
    elif hasattr(exc, "response") and exc.response is not None:
        try:
            raw = exc.response.text
        except Exception:  # noqa: BLE001
            raw = str(exc)
    return raw[:_SAFE_DETAIL_MAX]


@dataclass(frozen=True, slots=True)
class ToolkitInfo:
    """Minimal catalog entry for a Composio toolkit (app)."""

    slug: str
    name: str
    description: str
    auth_schemes: tuple[str, ...] = ()
    oauth_simple: bool = True
    managed_auth_available: bool | None = None
    setup_required: bool = False


@dataclass(frozen=True, slots=True)
class AuthConfigInfo:
    """Only non-secret metadata crosses the administration API boundary."""

    id: str
    toolkit_slug: str
    status: str


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """A single action exposed by a Composio toolkit."""

    slug: str
    description: str
    input_parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConnectedAccountInfo:
    """A user-connected account on Composio cloud."""

    id: str
    toolkit_slug: str
    entity_id: str
    status: str
    auth_config_id: str = ""


@dataclass(frozen=True, slots=True)
class ConnectionInitResult:
    """Result of initiating an OAuth connection."""

    connected_account_id: str
    redirect_url: str
    status: str


class ComposioApiError(Exception):
    """Raised when the Composio SDK call fails."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"Composio API {status_code}: {detail}")
        self.status_code = status_code


class ComposioClient:
    """Async Composio client backed by the official SDK.  One instance per API key."""

    def __init__(
        self, api_key: str, *, sdk: Composio | None = None,
        auth_config_ids: Mapping[str, str] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        # Accept an injected SDK for tests; otherwise construct with the real key.
        self._sdk: Composio = sdk if sdk is not None else Composio(api_key=api_key)
        # Server/owner-selected configuration only, never tool-call parameters.
        self._auth_config_ids = dict(auth_config_ids or {})
        # Composio v0.9+ REJECTS manual tools.execute() without a CONCRETE version
        # ("Toolkit version not specified" 502; "latest" is NOT accepted in manual
        # execution, nor as a global/dict default). So the agent could discover + call
        # Gmail/etc. but every execution failed. We resolve each toolkit's newest
        # published version (meta.available_versions[0]) once and cache it per slug —
        # the owner never has to pin versions by hand.
        self._ver_cache: dict[str, str] = {}

    # ----------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------

    async def _guarded(self, fn: Callable[[], _T]) -> _T:
        """Run a synchronous SDK call in a thread; translate SDK exceptions."""
        try:
            return await asyncio.to_thread(fn)
        except APIStatusError as exc:
            # Has a concrete status_code from the HTTP response.
            raise ComposioApiError(exc.status_code, _safe_detail(exc)) from exc
        except APIError as exc:
            # Connection/timeout errors — no status code available.
            raise ComposioApiError(502, str(exc)) from exc
        except ComposioError as exc:
            raise ComposioApiError(502, str(exc)) from exc

    # ----------------------------------------------------------------
    # Toolkit (app) catalog
    # ----------------------------------------------------------------

    async def list_toolkits(
        self,
        *,
        search: str | None = None,
        limit: int = 50,
    ) -> list[ToolkitInfo]:
        """Return available Composio toolkits (apps).

        No server-side search param exists; filtering is done in Python.
        """
        items = await self._guarded(
            lambda: self._sdk.toolkits.list(
                limit=float(limit),
                sort_by="usage",
            ).items
        )

        results: list[ToolkitInfo] = []
        for item in items:
            slug: str = item.slug or ""
            name: str = item.name or slug
            description: str = item.meta.description if item.meta else ""

            if not slug:
                continue

            if search:
                needle = search.lower()
                haystack = f"{slug} {name} {description}".lower()
                if needle not in haystack:
                    continue

            schemes = _extract_auth_schemes(item)
            managed = _managed_oauth_available(item)
            selected = bool(self._auth_config_ids.get(slug.lower()))
            simple = selected or (
                managed if managed is not None else
                False if slug.lower() in ADS_TOOLKITS else _is_oauth_simple(schemes)
            )
            results.append(
                ToolkitInfo(
                    slug=slug,
                    name=name,
                    description=description,
                    auth_schemes=schemes,
                    oauth_simple=simple,
                    managed_auth_available=managed,
                    setup_required=slug.lower() in ADS_TOOLKITS and not simple,
                )
            )

        return results

    async def assert_oauth_simple(self, toolkit_slug: str) -> None:
        """Raise ComposioApiError si el toolkit NO es OAuth-simple (clasificación
        compartida SO/TUI/agente). Fail-open: si no aparece en el catálogo o no se
        puede determinar el esquema, no bloquea.
        """
        slug = toolkit_slug.strip().lower()
        if slug in ADS_TOOLKITS:
            selected = self._auth_config_ids.get(slug)
            if selected:
                await self.validate_auth_config(slug, selected)
            else:
                await self._guarded(lambda: self._assert_managed_ads(slug))
            return
        toolkits = await self.list_toolkits(search=slug, limit=200)
        match = next((t for t in toolkits if t.slug.lower() == slug), None)
        if match is None:
            return  # desconocido → fail-open
        if not match.oauth_simple:
            schemes = ", ".join(match.auth_schemes) or "no-OAuth"
            raise ComposioApiError(
                400,
                f"«{match.name}» usa {schemes}; por ahora solo soportamos "
                f"integraciones con OAuth simple (un clic en el navegador).",
            )

    # ----------------------------------------------------------------
    # Tool list for a toolkit (cached 1h)
    # ----------------------------------------------------------------

    async def list_tools(self, toolkit_slug: str) -> list[ToolInfo]:
        """Return all actions for a toolkit.

        Results are cached per slug for _CACHE_TTL seconds.
        """
        lock = _get_cache_lock()
        async with lock:
            cached = _tool_cache.get(toolkit_slug)
            if cached and time.time() - cached[1] < _CACHE_TTL:
                return [ToolInfo(**t) for t in cached[0]]

        raw = await self._fetch_tools(toolkit_slug)
        serialisable = [
            {
                "slug": t.slug,
                "description": t.description,
                "input_parameters": t.input_parameters,
            }
            for t in raw
        ]
        async with lock:
            _tool_cache[toolkit_slug] = (serialisable, time.time())

        return raw

    async def _fetch_tools(self, toolkit_slug: str) -> list[ToolInfo]:
        sdk_tools = await self._guarded(
            lambda: self._sdk.tools.get_raw_composio_tools(
                toolkits=[toolkit_slug.upper()],
                limit=500,
            )
        )

        return [
            ToolInfo(
                slug=tool.slug,
                description=tool.description or getattr(tool, "human_description", "") or "",
                input_parameters=tool.input_parameters or {},
            )
            for tool in sdk_tools
            if tool.slug
        ]

    # ----------------------------------------------------------------
    # Connected accounts
    # ----------------------------------------------------------------

    async def list_connected_accounts(
        self, entity_id: str
    ) -> list[ConnectedAccountInfo]:
        """List active connected accounts for an entity."""
        response = await self._guarded(
            lambda: self._sdk.connected_accounts.list(
                user_ids=[entity_id],
                statuses=["ACTIVE"],
            )
        )

        return [
            ConnectedAccountInfo(
                id=item.id,
                toolkit_slug=item.toolkit.slug if item.toolkit else "",
                entity_id=item.user_id,
                status=item.status,
                auth_config_id=getattr(getattr(item, "auth_config", None), "id", ""),
            )
            for item in response.items
            if item.user_id == entity_id and item.status == "ACTIVE"
        ]

    async def get_connected_account(
        self, connection_id: str, *, entity_id: str,
    ) -> ConnectedAccountInfo:
        """Scoped status for OAuth confirmation; never returns state/tokens.

        Status can still be INITIATED. Callers admitting API execution must
        additionally require ACTIVE and the expected toolkit/auth-config.
        """
        def _get() -> ConnectedAccountInfo:
            account = self._sdk.connected_accounts.get(connection_id)
            if (
                getattr(account, "id", None) != connection_id
                or getattr(account, "user_id", None) != entity_id or not entity_id
            ):
                raise ComposioApiError(404, "No se encuentra esta conexión en tu espacio.")
            return ConnectedAccountInfo(
                id=account.id, entity_id=entity_id, status=account.status,
                toolkit_slug=getattr(getattr(account, "toolkit", None), "slug", ""),
                auth_config_id=getattr(getattr(account, "auth_config", None), "id", ""),
            )

        return await self._guarded(_get)

    async def initiate_connection(
        self,
        *,
        toolkit_slug: str,
        entity_id: str,
        redirect_url: str | None = None,
    ) -> ConnectionInitResult:
        """Start OAuth flow via Composio Connect Link.

        Uses `link()` instead of the retired `initiate()` for managed-auth
        OAuth flows.  Auth config is resolved or created once per call inside
        the same thread to keep it as a single to_thread boundary.
        """

        def _call() -> ConnectionInitResult:
            slug = toolkit_slug.strip().lower()
            selected = self._auth_config_ids.get(slug)
            if selected:
                auth_config_id = self._validate_auth_config(slug, selected).id
            else:
                if slug in ADS_TOOLKITS:
                    self._assert_managed_ads(slug)
                auth_config_id = self._resolve_managed_auth_config_id(slug)
            req = self._sdk.connected_accounts.link(
                entity_id,
                auth_config_id,
                callback_url=redirect_url,
                **({"allow_multiple": True} if slug in ADS_TOOLKITS else {}),
            )
            return ConnectionInitResult(
                connected_account_id=req.id,
                redirect_url=req.redirect_url or "",
                status=req.status,
            )

        return await self._guarded(_call)

    def _assert_managed_ads(self, toolkit_slug: str) -> None:
        toolkit = self._sdk.toolkits.get(slug=toolkit_slug)
        if (
            getattr(toolkit, "slug", "").lower() != toolkit_slug
            or getattr(toolkit, "enabled", False) is not True
            or _managed_oauth_available(toolkit) is not True
        ):
            raise ComposioApiError(
                409, "Falta preparar esta conexión en Safent. "
                "La administración debe seleccionar una configuración OAuth válida; "
                "no necesitas crear una aplicación ni copiar secretos.",
            )

    def _validate_auth_config(self, toolkit_slug: str, config_id: str) -> AuthConfigInfo:
        # get() is scoped by our Composio project API key. Do not serialize the
        # SDK object: it can contain credentials even though this method only
        # needs the public identity, scheme and status.
        try:
            config = self._sdk.auth_configs.get(config_id)
        except (APIError, ComposioError) as exc:
            # Auth-config SDK responses can include secret fields. Never echo
            # upstream error bodies to the owner/daemon/UI for this operation.
            raise ComposioApiError(409, "La configuración OAuth no está disponible.") from exc
        actual_slug = getattr(getattr(config, "toolkit", None), "slug", "")
        if (
            getattr(config, "id", None) != config_id
            or actual_slug != toolkit_slug
            or getattr(config, "status", None) != "ENABLED"
            or getattr(config, "auth_scheme", None) not in _OAUTH_SIMPLE_SCHEMES
        ):
            raise ComposioApiError(
                409, "La configuración OAuth no está disponible para esta plataforma. "
                "Pide a la administración que la revise.",
            )
        return AuthConfigInfo(id=config.id, toolkit_slug=actual_slug, status="ENABLED")

    async def validate_auth_config(self, toolkit_slug: str, config_id: str) -> AuthConfigInfo:
        return await self._guarded(lambda: self._validate_auth_config(toolkit_slug, config_id))

    async def resolve_ads_auth_config(self, toolkit_slug: str) -> AuthConfigInfo:
        """Prepare the shared Ads auth config without opening a user connection.

        Only a provider advertising managed OAuth may create a default. Meta's
        custom app must be selected explicitly by the owner, never guessed from
        unrelated project configs. The returned value contains no credentials.
        """
        if toolkit_slug not in ADS_TOOLKITS:
            raise ComposioApiError(409, "La plataforma no pertenece a Anuncios.")

        def resolve() -> AuthConfigInfo:
            config_id = self._auth_config_ids.get(toolkit_slug)
            if not config_id:
                self._assert_managed_ads(toolkit_slug)
                config_id = self._resolve_managed_auth_config_id(toolkit_slug)
            return self._validate_auth_config(toolkit_slug, config_id)

        return await self._guarded(resolve)

    def _resolve_managed_auth_config_id(self, toolkit_slug: str) -> str:
        """Return an existing enabled managed auth config ID, or create one.

        Must be called from a worker thread (synchronous SDK calls).
        """
        configs = self._sdk.auth_configs.list(
            toolkit_slug=toolkit_slug.lower(),
            is_composio_managed=True,
        )

        for item in configs.items:
            if item.status != "DISABLED":
                return item.id

        created = self._sdk.auth_configs.create(
            toolkit_slug.lower(),
            {"type": "use_composio_managed_auth"},
        )
        return created.id

    async def delete_connection(self, connection_id: str, *, entity_id: str) -> None:
        """Delete a connected account by ID."""
        def _delete() -> None:
            account = self._sdk.connected_accounts.get(connection_id)
            if (
                getattr(account, "id", None) != connection_id
                or getattr(account, "user_id", None) != entity_id or not entity_id
            ):
                raise ComposioApiError(404, "No se encuentra esta conexión en tu espacio.")
            self._sdk.connected_accounts.delete(connection_id)

        await self._guarded(_delete)

    async def _latest_toolkit_version(self, tool_slug: str) -> str | None:
        """Newest concrete version of the toolkit owning `tool_slug`, cached per toolkit.

        GMAIL_FETCH_EMAILS -> toolkit 'gmail' -> meta.available_versions[0] (e.g.
        '20260624_00'). Composio v0.9+ rejects manual execute() without a concrete
        version and does NOT accept 'latest' there, so we resolve the latest published
        version once per toolkit. Best-effort: on any error return None (no version).
        """
        toolkit = (tool_slug or "").split("_", 1)[0].lower()
        if not toolkit:
            return None
        if toolkit in self._ver_cache:
            return self._ver_cache[toolkit]
        try:
            tk = await self._guarded(lambda: self._sdk.toolkits.get(slug=toolkit))
            versions = list(getattr(getattr(tk, "meta", None), "available_versions", []) or [])
            ver = versions[0] if versions else None
        except Exception:  # noqa: BLE001 — best-effort; fall through to no version
            ver = None
        if ver:
            self._ver_cache[toolkit] = ver
        return ver

    # ----------------------------------------------------------------
    # Action execution
    # ----------------------------------------------------------------

    async def execute_action(
        self,
        *,
        slug: str,
        params: dict[str, Any],
        entity_id: str,
        connected_account_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a Composio action and return the action data.

        Returns the `data` dict on success.  Raises `ComposioApiError`
        on execution failure so the LLM sees a structured error, not a
        raw dict with ``successful=False``.

        connected_account_id: when provided, disambiguates between multiple
        connected accounts for the same toolkit on the same entity.  Both
        connected_account_id and user_id are passed so the entity scope is
        maintained while the exact account is pinned.  None → current behaviour.
        """
        _ca_id = connected_account_id or None  # explicit None keeps SDK default
        # Composio v0.9+ requires a concrete toolkit version for manual execute().
        version = await self._latest_toolkit_version(slug)
        _ver_kw = {"version": version} if version else {}
        resp = await self._guarded(
            lambda: self._sdk.tools.execute(
                slug,
                params,
                user_id=entity_id,
                connected_account_id=_ca_id,
                **_ver_kw,
            )
        )

        if not resp.get("successful"):
            raise ComposioApiError(
                502,
                resp.get("error") or "tool execution failed",
            )

        return resp.get("data") or {}
