"""ComposioToolsRegistry: TTL-cached dynamic source of Composio ToolSpecs.

Solves the "tools frozen at startup" bug: Composio integrations connected
AFTER the daemon started are picked up on the next cycle, within TTL seconds.

Design:
  - On each call to get_composio_tools(), if the cache is fresh (monotonic
    time < TTL), return the last-good tuple immediately (no network call).
  - If stale, re-read the credential from DB (handles credential appearing
    after first boot) and re-fetch connected accounts from Composio cloud.
  - If the refresh fails (network down, Composio API error), serve the last
    good cache and log WARN — never raise, never drop native tools.
  - If no credential is present, return () and reset the stale timestamp so
    it will be re-checked next cycle (credential might appear later).

TTL is configurable via HERMES_COMPOSIO_TOOLS_TTL_S (default 30 s).

Security: the api_key is held only in memory inside ComposioCredential,
never logged. get_composio_tools() returns only ToolSpec objects — no keys.

Thread/async safety: the registry is designed for single-threaded async use
(one daemon, one event loop). A simple asyncio.Lock prevents concurrent
refreshes from hammering the Composio API when two cycles arrive at the
same stale moment.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Awaitable
from pathlib import Path
from typing import Any

from hermes.domain.tool_spec import ToolSpec

logger = logging.getLogger("hermes.runtime.composio_tools_registry")

_DEFAULT_TTL_S = 30.0
_REFRESH_TIMEOUT_S = 10.0  # bounded per-refresh so a slow Composio API never blocks a cycle

# Type aliases for the injectable callables (primarily for testability).
CredentialLoader = Callable[[Path], Any]  # (db_path) -> ComposioCredential | None
ToolsBuilder = Callable[[Any], Awaitable[tuple[ToolSpec, ...]]]  # (credential) -> specs


def _default_credential_loader(db_path: Path) -> Any:
    """Production default: load credential from shell-state.db."""
    try:
        from hermes.runtime.composio_config_source import load_composio_credential  # noqa: PLC0415
        return load_composio_credential(db_path)
    except Exception:  # noqa: BLE001
        logger.debug("hermes.composio_tools_registry.config_module_unavailable")
        return None


async def _default_tools_builder(credential: Any) -> tuple[ToolSpec, ...]:
    """Broker-less fallback: raises unconditionally.

    build_composio_tool_specs now requires a broker (KC-4: all Composio READ
    actions must route through broker.dispatch for consent+audit+kill-switch).
    A broker-less ToolsBuilder is unconstructable by design.

    Always inject a broker-aware tools_builder via ComposioToolsRegistry(tools_builder=...)
    or use _build_composio_registry_with_broker() which injects the broker closure.
    This default is intentionally fail-closed to surface wiring bugs at construction
    time rather than silently building specs whose READ handlers bypass the broker.
    """
    raise RuntimeError(
        "hermes.composio_tools_registry._default_tools_builder: "
        "broker-less Composio tool spec construction is unconstructable. "
        "Wire broker+consent_context via ComposioToolsRegistry(tools_builder=_broker_aware_fn) "
        "or use _build_composio_registry_with_broker()."
    )


class ComposioToolsRegistry:
    """Live-reloading cache of Composio ToolSpecs.

    Args:
        db_path: Path to shell-state.db (credentials are read from here).
        ttl_s: Cache TTL in seconds. After expiry the next call re-fetches
               connected accounts from Composio cloud. Defaults to
               HERMES_COMPOSIO_TOOLS_TTL_S env var, or 30 s.
        credential_loader: Injectable for tests. Defaults to reading shell-state.db.
        tools_builder: Injectable for tests. Defaults to build_composio_tool_specs.

    Usage:
        registry = ComposioToolsRegistry(db_path=Path("/var/lib/hermes/shell-state.db"))
        # In run_cycle:
        composio_tools = await registry.get_composio_tools()
        all_tools = native_tools + composio_tools
    """

    def __init__(
        self,
        *,
        db_path: Path,
        ttl_s: float | None = None,
        credential_loader: CredentialLoader | None = None,
        tools_builder: ToolsBuilder | None = None,
    ) -> None:
        self._db_path = db_path
        self._ttl_s = ttl_s if ttl_s is not None else float(
            os.environ.get("HERMES_COMPOSIO_TOOLS_TTL_S", str(_DEFAULT_TTL_S))
        )
        self._load_credential = credential_loader or _default_credential_loader
        self._build_tools = tools_builder or _default_tools_builder
        self._cached: tuple[ToolSpec, ...] = ()
        # 0.0 means "never populated" — triggers an immediate refresh on first call.
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_composio_tools(self) -> tuple[ToolSpec, ...]:
        """Return the current Composio ToolSpec set, refreshing if stale.

        Never raises. On error returns last-good cache (or empty on first call).
        """
        now = time.monotonic()
        if now - self._cached_at < self._ttl_s:
            return self._cached

        async with self._lock:
            # Re-check inside lock — another coroutine may have refreshed while
            # we were waiting to acquire it.
            now = time.monotonic()
            if now - self._cached_at < self._ttl_s:
                return self._cached

            await self._refresh(now)
            return self._cached

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _refresh(self, now: float) -> None:
        """Fetch fresh Composio tools. On any failure, keep last-good cache."""
        try:
            fresh = await asyncio.wait_for(
                self._fetch_tools(), timeout=_REFRESH_TIMEOUT_S
            )
            app_count = _count_apps(fresh)
            # DEBUG, no INFO: se emite en cada refresco por TTL → a INFO inundaba
            # el journal y tapaba los logs reales (chat/LLM). El scope ya es
            # SOLO cuentas conectadas ACTIVE (build_composio_tool_specs), no el
            # catálogo global — no llena el contexto del agente.
            logger.debug(
                "hermes.composio_tools_registry.refreshed",
                extra={"tool_count": len(fresh), "app_count": app_count},
            )
            self._cached = fresh
            # Advance the timestamp even if the set is empty so we don't
            # hammer the API every cycle when no apps are connected.
            self._cached_at = now
        except asyncio.TimeoutError:
            logger.warning(
                "hermes.composio_tools_registry.refresh_timeout "
                "timeout_s=%.1f — serving last-good cache (%d tools)",
                _REFRESH_TIMEOUT_S,
                len(self._cached),
            )
            # Do NOT advance _cached_at: retry next cycle.
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.composio_tools_registry.refresh_failed: %s "
                "— serving last-good cache (%d tools)",
                exc,
                len(self._cached),
            )
            # Do NOT advance _cached_at: retry next cycle.

    async def _fetch_tools(self) -> tuple[ToolSpec, ...]:
        """Load credential + build ToolSpecs. Returns () if no credential."""
        credential = self._load_credential(self._db_path)
        if credential is None:
            logger.debug("hermes.composio_tools_registry.no_credential")
            return ()
        return await self._build_tools(credential)


def _count_apps(specs: tuple[ToolSpec, ...]) -> int:
    """Count distinct Composio app slugs (first segment of tool name)."""
    return len({s.name.split("_")[0] for s in specs if s.name})
