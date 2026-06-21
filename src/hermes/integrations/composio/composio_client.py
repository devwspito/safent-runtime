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
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

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
        mode = getattr(c, "mode", None) or getattr(c, "auth_mode", None) or getattr(c, "scheme", None) or c
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

    def __init__(self, api_key: str, *, sdk: Composio | None = None) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        # Accept an injected SDK for tests; otherwise construct with the real key.
        self._sdk: Composio = sdk if sdk is not None else Composio(api_key=api_key)

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
            results.append(
                ToolkitInfo(
                    slug=slug,
                    name=name,
                    description=description,
                    auth_schemes=schemes,
                    oauth_simple=_is_oauth_simple(schemes),
                )
            )

        return results

    async def assert_oauth_simple(self, toolkit_slug: str) -> None:
        """Raise ComposioApiError si el toolkit NO es OAuth-simple (clasificación
        compartida SO/TUI/agente). Fail-open: si no aparece en el catálogo o no se
        puede determinar el esquema, no bloquea.
        """
        slug = toolkit_slug.strip().lower()
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
            )
            for item in response.items
        ]

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
            auth_config_id = self._resolve_managed_auth_config_id(toolkit_slug)
            req = self._sdk.connected_accounts.link(
                entity_id,
                auth_config_id,
                callback_url=redirect_url,
            )
            return ConnectionInitResult(
                connected_account_id=req.id,
                redirect_url=req.redirect_url or "",
                status=req.status,
            )

        return await self._guarded(_call)

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

    async def delete_connection(self, connection_id: str) -> None:
        """Delete a connected account by ID."""
        await self._guarded(
            lambda: self._sdk.connected_accounts.delete(connection_id)
        )

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
        resp = await self._guarded(
            lambda: self._sdk.tools.execute(
                slug,
                params,
                user_id=entity_id,
                connected_account_id=_ca_id,
            )
        )

        if not resp.get("successful"):
            raise ComposioApiError(
                502,
                resp.get("error") or "tool execution failed",
            )

        return resp.get("data") or {}
