"""MemorySurfaceAdapter — SurfaceAdapterPort for agent memory writes (F4).

Executes `memory` tool proposals (action: add/replace/remove) for the
SurfaceKind.MEMORY surface after broker routing.

Security properties:
  - Tenant-confined: all writes go to /var/lib/hermes/memory/<tenant_id>/.
    No cross-tenant access is possible (path traversal prevention in
    TenantMemoryStore._entry_path via resolve() + prefix check).
  - PII gated: entries are scanned before writing. PII → EXECUTED_FAILED.
  - Audited: the broker writes an AuditKind.PROPOSAL_EXECUTED entry after
    every dispatch, including memory writes.
  - LOW + auto_executable: no HITL because memory is internal agent state,
    reversible, with no external system effect. Governed by audit trail.

Capa: infrastructure (wraps TenantMemoryStore). DIP: depends on the port
SurfaceAdapterPort (domain layer). No framework.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
    SurfaceAdapterPort,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.memory.infrastructure.tenant_memory_store import (
    PiiRejectedError,
    TenantMemoryError,
    TenantMemoryStore,
)

logger = logging.getLogger(__name__)

_DEFAULT_MEMORY_ROOT = Path("/var/lib/hermes/memory")


class MemorySurfaceAdapter:
    """SurfaceAdapterPort for the MEMORY surface.

    Injected into SurfaceAdapterDispatcher under SurfaceKind.MEMORY.
    Called by CapabilityBroker.dispatch() for auto-executable memory proposals.

    Args:
        memory_root: Base directory for tenant memory files.
                     Default: /var/lib/hermes/memory.
                     Overridable via HERMES_MEMORY_ROOT env or constructor.
    """

    def __init__(self, *, memory_root: Path | None = None) -> None:
        self._memory_root = memory_root or _DEFAULT_MEMORY_ROOT

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.MEMORY

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> CapturedAction:
        """Not used directly — memory proposals originate from Nous tool calls."""
        return CapturedAction(
            surface_kind=self.surface_kind,
            intent_desc=intent_desc,
            payload=params,
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
        )

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        """Execute the memory write, confined to the action's tenant_id."""
        if action.surface_kind != self.surface_kind:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=f"MemorySurfaceAdapter cannot handle surface_kind={action.surface_kind!r}",
            )

        mem_action = action.payload.get("action", "")
        target = action.payload.get("target", "memory")
        content = action.payload.get("content", "")
        old_text = action.payload.get("old_text", "")
        # Provenance: injected by _gated_memory_tool under a reserved key.
        # Falls back to "unknown" — fail-soft, never blocks the write.
        agent_id: str = str(action.payload.get("_provenance_agent_id") or "unknown")

        tenant_id = action.tenant_id or UUID(int=0)
        store = TenantMemoryStore(root=self._memory_root, tenant_id=tenant_id)

        try:
            return self._dispatch_action(action, store, mem_action, target, content, old_text, agent_id)
        except PiiRejectedError as exc:
            logger.warning(
                "hermes.memory_adapter.pii_rejected tenant=%s action=%s: %s",
                str(tenant_id)[:8],
                mem_action,
                str(exc),
            )
            return ReplayOutcome.failed(action.action_id, error=str(exc))
        except TenantMemoryError as exc:
            logger.error(
                "hermes.memory_adapter.store_error tenant=%s: %s",
                str(tenant_id)[:8],
                str(exc),
            )
            return ReplayOutcome.failed(action.action_id, error=str(exc))

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    def _dispatch_action(
        self,
        action: CapturedAction,
        store: TenantMemoryStore,
        mem_action: str,
        target: str,
        content: str,
        old_text: str,
        agent_id: str = "unknown",
    ) -> ReplayOutcome:
        if mem_action == "add":
            result = store.add(target, content, agent_id=agent_id)
        elif mem_action == "replace":
            result = store.replace(target, old_text, content, agent_id=agent_id)
        elif mem_action == "remove":
            result = store.remove(target, old_text)
        else:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=f"memory action={mem_action!r} not supported",
            )

        if result.get("success"):
            logger.info(
                "hermes.memory_adapter.%s tenant=%s target=%s entries=%s",
                mem_action,
                str(action.tenant_id or "")[:8],
                target,
                result.get("entry_count", "?"),
            )
            return ReplayOutcome.ok(action.action_id, result=result)

        return ReplayOutcome.failed(action.action_id, error=result.get("error", "unknown"))
