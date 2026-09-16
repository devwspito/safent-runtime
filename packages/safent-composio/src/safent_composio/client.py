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
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from composio.exceptions import ComposioError
from composio_client import APIError, APIStatusError

from safent_composio._classification import (
    ADS_TOOLKITS,
    _OAUTH_SIMPLE_SCHEMES,
    _extract_auth_schemes,
    _is_oauth_simple,
    _managed_oauth_available,
)
from safent_composio._transport import SdkHandle, build_sdk
from safent_composio.errors import ComposioApiError, extract_detail
from safent_composio.values import (
    AuthConfigInfo,
    ConnectedAccountInfo,
    ConnectionInitResult,
    ToolInfo,
    ToolkitInfo,
)

_CACHE_TTL = 3600  # 1 hour, mirrors agents-autonomy/tool_catalog.py
_NOT_FOUND_STATUS_CODES = frozenset({404, 410})

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


class ComposioClient:
    """Async Composio client backed by the official SDK.  One instance per API key."""

    def __init__(
        self,
        *,
        api_key: str,
        auth_config_ids: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sdk: SdkHandle | None = None,
    ) -> None:
        """
        auth_config_ids: server/owner-selected toolkit_slug -> auth_config_id
            map, never tool-call parameters. When a slug is present,
            `initiate_connection` uses it directly instead of listing/creating
            a managed auth config on Composio.
        transport: injected httpx transport (Enterprise's anti-SSRF boundary —
            fixed host, no env proxies, no redirects). `None` keeps today's
            default: the SDK's own httpx client. See `_transport.build_sdk`.
        sdk: full SDK substitute for tests. Mutually exclusive with
            `transport` — a test that fakes the whole SDK has no use for a
            real transport, and silently ignoring one of the two would hide
            a caller bug.
        """
        if not api_key:
            raise ValueError("api_key is required")
        if sdk is not None and transport is not None:
            raise ValueError("sdk and transport are mutually exclusive")
        self._auth_config_ids = {k.lower(): v for k, v in (auth_config_ids or {}).items()}
        self._sdk: SdkHandle = sdk if sdk is not None else build_sdk(api_key, transport=transport)
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
            raise ComposioApiError(exc.status_code, extract_detail(exc)) from exc
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

    async def list_connected_accounts(self, entity_id: str) -> list[ConnectedAccountInfo]:
        """List active connected accounts for an entity."""
        response = await self._guarded(
            lambda: self._sdk.connected_accounts.list(
                user_ids=[entity_id],
                statuses=["ACTIVE"],
            )
        )
        return [
            _to_connected_account_info(item)
            for item in response.items
            if item.user_id == entity_id and item.status == "ACTIVE"
        ]

    async def get_connected_account(
        self, connection_id: str, *, entity_id: str
    ) -> ConnectedAccountInfo:
        """Scoped status for OAuth confirmation; never returns state/tokens.

        Status can still be INITIATED. Callers admitting API execution must
        additionally require ACTIVE and the expected toolkit/auth-config.

        Raises ComposioApiError(404, ...) if the id doesn't exist OR belongs
        to a different entity — callers cannot tell the two apart, by design
        (no account-enumeration oracle across seats).
        """

        def _get() -> ConnectedAccountInfo:
            account = self._sdk.connected_accounts.get(connection_id)
            if _not_owned_by(account, connection_id, entity_id):
                raise ComposioApiError(404, "No se encuentra esta conexión en tu espacio.")
            return _to_connected_account_info(account)

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

    async def prepare_meta_auth_config(
        self, *, client_id: str, client_secret: str
    ) -> AuthConfigInfo:
        """Owner-only setup. Secret goes straight to Composio, never into local storage.

        A deterministic name recovers a successful remote create after a lost
        response. Existing configurations are never updated or deleted here.
        """

        def prepare() -> AuthConfigInfo:
            try:
                name = f"Safent Meta {client_id}"
                configs = self._sdk.auth_configs.list(
                    toolkit_slug="metaads", is_composio_managed=False,
                    search=name, limit=1000,
                )
                matches = [item for item in configs.items if getattr(item, "name", None) == name]
                if len(matches) > 1:
                    raise ComposioApiError(409, "Hay varias configuraciones de esta aplicación.")
                if matches:
                    return self._validate_auth_config("metaads", matches[0].id)
                # Toolkit-specific credential fields are advertised by the toolkit
                # definition; the generated SDK only types common OAuth fields.
                options: Any = {
                    "type": "use_custom_auth", "auth_scheme": "OAUTH2", "name": name,
                    "is_enabled_for_tool_router": False,
                    "credentials": {
                        "client_id": client_id, "client_secret": client_secret,
                        "oauth_redirect_uri": "https://backend.composio.dev/api/v1/auth-apps/add",
                        "scopes": "ads_read,ads_management,business_management",
                    },
                }
                created = self._sdk.auth_configs.create("metaads", options)
                return self._validate_auth_config("metaads", created.id)
            except (APIError, ComposioError) as exc:
                # Neither SDK errors nor response bodies may echo app credentials.
                raise ComposioApiError(502, "No se pudo preparar Meta en Composio.") from exc

        return await self._guarded(prepare)

    def _resolve_managed_auth_config_id(self, toolkit_slug: str) -> str:
        """Return a known, existing enabled, or newly created managed auth config ID.

        Must be called from a worker thread (synchronous SDK calls).
        """
        slug = toolkit_slug.lower()
        known = self._auth_config_ids.get(slug)
        if known:
            return known

        configs = self._sdk.auth_configs.list(
            toolkit_slug=slug,
            is_composio_managed=True,
        )

        for item in configs.items:
            if item.status != "DISABLED":
                return item.id

        created = self._sdk.auth_configs.create(
            slug,
            {"type": "use_composio_managed_auth"},
        )
        return created.id

    async def delete_connection(self, connection_id: str, *, entity_id: str) -> None:
        """Delete a connected account by ID.

        Idempotent (REQ-11): a provider 404/410 while confirming ownership is
        already a successful revocation — nothing live remains under this id.
        An ownership mismatch (right id, wrong entity) is a real, non-idempotent
        404: the caller must not believe they revoked someone else's connection.
        """
        try:
            account = await self._guarded(
                lambda: self._sdk.connected_accounts.get(connection_id)
            )
        except ComposioApiError as exc:
            if exc.status_code in _NOT_FOUND_STATUS_CODES:
                return
            raise
        if _not_owned_by(account, connection_id, entity_id):
            raise ComposioApiError(404, "No se encuentra esta conexión en tu espacio.")

        try:
            await self._guarded(lambda: self._sdk.connected_accounts.delete(connection_id))
        except ComposioApiError as exc:
            if exc.status_code not in _NOT_FOUND_STATUS_CODES:
                raise

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


def _not_owned_by(account: Any, connection_id: str, entity_id: str) -> bool:
    return (
        getattr(account, "id", None) != connection_id
        or getattr(account, "user_id", None) != entity_id
        or not entity_id
    )


def _to_connected_account_info(item: Any) -> ConnectedAccountInfo:
    return ConnectedAccountInfo(
        id=item.id,
        toolkit_slug=item.toolkit.slug if item.toolkit else "",
        entity_id=item.user_id,
        status=item.status,
        auth_config_id=getattr(getattr(item, "auth_config", None), "id", ""),
    )
