"""DbusInstallExecutor — adapter that maps install/search tool calls to the
DbusRuntimeServiceWiring functions that already scan + authenticate.

Layer: infrastructure (thin adapter, I/O boundary).

Security contract:
  - The broker has already run kill-switch, registry lookup, taint classification,
    consent gate, HITL gate, idempotency, and audit-signing BEFORE reaching here.
  - owner_uid resolves to the device owner (hermes-user, uid≥1000) so that
    wiring._authorize_and_resolve accepts the call — same uid used by the D-Bus
    channel.  Risk: if the wiring later adds a secondary check we propagate the
    same uid consistently.
  - Scan gates are INSIDE the wiring functions (add_mcp_server, install_hub_skill,
    install_package).  We do NOT bypass _scan_install_target.
  - Every blocking/non-OK result maps to REJECTED_BY_POLICY or EXECUTED_FAILED.
    We never swallow failures.

OAuth-simple limit (connect_integration):
  Composio's connect_composio_app returns a redirect/connect link for OAuth-simple
  apps (OAUTH2/OAUTH1).  Non-OAuth apps surface the error string from the wiring
  directly to the caller so the LLM can report it cleanly.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from hermes.agents_os.domain.ports.surface_adapter_port import ReplayOutcome, ReplayStatus
from hermes.capabilities.application.install_executor import InstallExecutorPort

if TYPE_CHECKING:
    from hermes.agents_os.domain.ports.surface_adapter_port import CapturedAction
    from hermes.agents_os.infrastructure.dbus_runtime_service import DbusRuntimeServiceWiring
    from hermes.domain.proposal import ToolCallProposal

logger = logging.getLogger("hermes.capabilities.install_executor")

# -------------------------------------------------------------------------
# Tool name constants (single source of truth — avoids typos)
# -------------------------------------------------------------------------
_SEARCH_MCP = "search_mcp"
_SEARCH_SKILLS = "search_skills"
_SEARCH_APPS = "search_apps"
_INSTALL_MCP = "install_mcp"
_INSTALL_SKILL = "install_skill"
_INSTALL_APP = "install_app"
_CONNECT_INTEGRATION = "connect_integration"


class DbusInstallExecutor:
    """Infrastructure adapter: routes install/search proposals to wiring funcs.

    Construction is two-step (same pattern as AppLaunchSurfaceAdapter):
      1. Instantiate with wiring=None before the D-Bus adapter starts.
      2. Call set_wiring(wiring) after the wiring is built so the executor can
         reach the live wiring functions.

    Args:
        wiring:    DbusRuntimeServiceWiring — the shared wiring instance used
                   by the D-Bus adapter.  None means the executor is inactive
                   (fail-closed by the broker before reaching here, but we add
                   a safety net inside execute() as well).
        owner_uid: POSIX uid of the device owner (hermes-user).  Passed as
                   sender_uid so the wiring's authZ + scan run normally.
    """

    def __init__(
        self,
        *,
        wiring: DbusRuntimeServiceWiring | None,
        owner_uid: int,
    ) -> None:
        self._wiring = wiring
        self._owner_uid = owner_uid

    def set_wiring(self, wiring: DbusRuntimeServiceWiring) -> None:
        """Deferred wiring injection (step 2 of two-step construction)."""
        self._wiring = wiring

    async def execute(
        self,
        proposal: ToolCallProposal,
        action: CapturedAction,
    ) -> ReplayOutcome:
        """Dispatch by tool_name.  Fail-closed: unknown tool → REJECTED_BY_POLICY."""
        if self._wiring is None:
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error="install_executor: wiring no inyectado — fail-closed",
            )

        t0 = time.monotonic()
        tool = proposal.tool_name
        params = proposal.parameters

        try:
            if tool == _SEARCH_MCP:
                result = await self._search_mcp(params)
            elif tool == _SEARCH_SKILLS:
                result = await self._search_skills(params)
            elif tool == _SEARCH_APPS:
                result = await self._search_apps(params)
            elif tool == _INSTALL_MCP:
                result = await self._install_mcp(params)
            elif tool == _INSTALL_SKILL:
                result = await self._install_skill(params)
            elif tool == _INSTALL_APP:
                result = await self._install_app(params)
            elif tool == _CONNECT_INTEGRATION:
                result = await self._connect_integration(params)
            else:
                return ReplayOutcome(
                    action_id=action.action_id,
                    status=ReplayStatus.REJECTED_BY_POLICY,
                    error=f"install_executor: tool desconocido '{tool}'",
                )
        except Exception as exc:  # noqa: BLE001 — infrastructure boundary
            logger.error(
                "hermes.install_executor.unexpected_error tool=%s error=%s",
                tool,
                exc,
                exc_info=True,
            )
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.EXECUTED_FAILED,
                error=f"install_executor: error inesperado — {exc}",
            )

        duration_ms = int((time.monotonic() - t0) * 1000)
        return self._build_outcome(action, result, duration_ms)

    # ------------------------------------------------------------------
    # Search verbs — READ, no scan, no authZ required by wiring
    # ------------------------------------------------------------------

    async def _search_mcp(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query", "")).strip()
        limit = int(params.get("limit", 20))
        results = await self._wiring.search_mcp_registry(query=query, limit=limit)  # type: ignore[union-attr]
        return {"results": results, "count": len(results)}

    async def _search_skills(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query", "")).strip()
        source = str(params.get("source", "all"))
        limit = int(params.get("limit", 20))
        return self._wiring.search_skills_hub(  # type: ignore[union-attr]
            query=query, source=source, limit=limit
        )

    async def _search_apps(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query", "")).strip()
        source = str(params.get("source", "all"))
        results = self._wiring.search_packages(query=query, source=source)  # type: ignore[union-attr]
        return {"results": results, "count": len(results)}

    # ------------------------------------------------------------------
    # Install verbs — HIGH, scan inside wiring, authZ via owner_uid
    # ------------------------------------------------------------------

    async def _install_mcp(self, params: dict[str, Any]) -> dict[str, Any]:
        server_id = str(params.get("server_id", "")).strip()
        argv = [str(a) for a in (params.get("argv") or []) if str(a).strip()]
        env = params.get("env") or {}
        draft = {"server_id": server_id, "argv": argv, "env": env}
        return await self._wiring.add_mcp_server(  # type: ignore[union-attr]
            draft_json=json.dumps(draft),
            sender_uid=self._owner_uid,
        )

    async def _install_skill(self, params: dict[str, Any]) -> dict[str, Any]:
        identifier = str(params.get("identifier", "")).strip()
        return self._wiring.install_hub_skill(  # type: ignore[union-attr]
            identifier=identifier,
            sender_uid=self._owner_uid,
        )

    async def _install_app(self, params: dict[str, Any]) -> dict[str, Any]:
        source = str(params.get("source", "")).strip()
        package_id = str(params.get("package_id", "")).strip()
        return self._wiring.install_package(  # type: ignore[union-attr]
            source=source,
            package_id=package_id,
            sender_uid=self._owner_uid,
        )

    async def _connect_integration(self, params: dict[str, Any]) -> dict[str, Any]:
        slug = str(params.get("slug", "")).strip()
        return await self._wiring.connect_composio_app(  # type: ignore[union-attr]
            toolkit_slug=slug,
            sender_uid=self._owner_uid,
        )

    # ------------------------------------------------------------------
    # Result → ReplayOutcome mapping
    # ------------------------------------------------------------------

    def _build_outcome(
        self,
        action: CapturedAction,
        result: dict[str, Any],
        duration_ms: int,
    ) -> ReplayOutcome:
        """Map a wiring result dict to a ReplayOutcome.

        Mapping rules:
          {"blocked": True}           → REJECTED_BY_POLICY (scan blocked)
          {"ok": False, ...}          → EXECUTED_FAILED
          {"error": ..., no "ok"}     → EXECUTED_FAILED
          {"ok": True} / any positive → EXECUTED_OK
        """
        if result.get("blocked"):
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error=result.get("error", "instalación bloqueada por Centro de Seguridad"),
                duration_ms=duration_ms,
            )

        if "ok" in result and not result["ok"]:
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.EXECUTED_FAILED,
                result=result,
                error=result.get("error", "operación fallida"),
                duration_ms=duration_ms,
            )

        if "ok" not in result and "error" in result and not result.get("results") and not result.get("count"):
            # Error-only response without an explicit ok field (e.g. identifier vacío)
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.EXECUTED_FAILED,
                result=result,
                error=result.get("error", "operación fallida"),
                duration_ms=duration_ms,
            )

        return ReplayOutcome(
            action_id=action.action_id,
            status=ReplayStatus.EXECUTED_OK,
            result=result,
            duration_ms=duration_ms,
        )


# Protocol satisfaction assertion (application-layer contract check)
assert isinstance(DbusInstallExecutor(wiring=None, owner_uid=1000), InstallExecutorPort)
