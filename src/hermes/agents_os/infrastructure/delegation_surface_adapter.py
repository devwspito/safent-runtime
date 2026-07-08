"""DelegationSurfaceAdapter — FASE 3 (A2A cross-human), REQUESTER side.

Implements `SurfaceAdapterPort` for `SurfaceKind.PEER_DELEGATION`: the
`delegate_to_colleague` capability's execution. Registered in the
CapabilityRegistry as risk=HIGH / auto_executable=False (`capability_
registry.py`) — every call MUST already have passed the broker's consent +
HITL gate (Constitución II/IV) before `replay()` is ever invoked; this
adapter performs ZERO additional authorization of its own (that would
duplicate — and risk drifting from — the broker's single choke-point).

Diseño:
- `replay()` (the real runtime path — `capture()` is the offline teaching
  analogue, mirroring `ApiCallSurfaceAdapter`) mints a FRESH `correlation_id`
  and POSTs `{to_employee_id, to_agent_id: "", body, correlation_id}` to
  `{cloud}/v1/outbox` (Bearer instance_secret, HTTPS-only, no redirects) —
  the cloud notarises the request (signs it with the tenant key) and routes
  it to the target human's assistant.
- Records `correlation_id -> conversation_id` (via `config_sync.delegation_
  inbox.record_delegation_correlation`) so a LATER `kind=result` envelope can
  be delivered back into THIS conversation — `conversation_id` is resolved
  from `action.work_item_id` (the CURRENT task) via a direct read of
  `agent_tasks.conversation_id`, since `SurfaceAdapterPort.replay()` does not
  carry conversation_id explicitly. Best-effort: an autonomous (non-chat) task
  has no conversation to bind — the delegation still succeeds, only the
  automatic "result lands back in this thread" convenience is unavailable.
- URL/host is NOT configurable per-call (unlike `ApiCallSurfaceAdapter`): the
  cloud_endpoint comes exclusively from the paired `SQLiteAssociationStore`
  (the SAME endpoint config_sync/remote_approvals use) — the agent can never
  redirect this call to an arbitrary host (anti-SSRF by construction, no
  allowlist needed because there is no attacker-controlled URL parameter).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.instance.infrastructure.http_control_plane_client import (
    _validate_cloud_endpoint,
)
from hermes.instance.pairing_service import PairingError

logger = logging.getLogger("hermes.agents_os.delegation_surface_adapter")

_HTTP_TIMEOUT_S = 20.0


class DelegationSurfaceAdapter:
    """Cumple `SurfaceAdapterPort` para superficie `PEER_DELEGATION`."""

    def __init__(
        self,
        *,
        association_store: Any,
        db_path: Path,
        timeout_s: float = _HTTP_TIMEOUT_S,
    ) -> None:
        self._store = association_store
        self._db_path = db_path
        self._timeout_s = timeout_s

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.PEER_DELEGATION

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: Any,
        human_operator_id: Any,
    ) -> CapturedAction:
        employee_id, task, error = _validate_params(params, self._directory_employee_ids())
        result = (
            {}
            if error
            else await self._post_outbox(employee_id=employee_id, task=task)
        )
        return CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.PEER_DELEGATION,
            intent_desc=intent_desc,
            payload={
                "employee_id": employee_id,
                "task": task,
                "correlation_id": result.get("correlation_id", ""),
            },
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
        if action.surface_kind != SurfaceKind.PEER_DELEGATION:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=(
                    f"surface mismatch: esperado PEER_DELEGATION, "
                    f"got {action.surface_kind}"
                ),
            )
        employee_id, task, error = _validate_params(
            action.payload, self._directory_employee_ids()
        )
        if error:
            return ReplayOutcome.failed(action.action_id, error=error)

        result = await self._post_outbox(employee_id=employee_id, task=task)
        if not result.get("ok"):
            return ReplayOutcome.failed(
                action.action_id,
                error=result.get("error", "delegate_to_colleague: fallo de entrega"),
            )

        correlation_id = result["correlation_id"]
        conversation_id = self._resolve_conversation_id(action.work_item_id)
        if conversation_id:
            _record_correlation(
                db_path=self._db_path,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
            )
        else:
            logger.info(
                "hermes.delegation_surface_adapter.no_conversation_to_bind",
                extra={"correlation_id": correlation_id},
            )

        return ReplayOutcome.ok(
            action.action_id, result={"correlation_id": correlation_id}
        )

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        canonical = {
            "surface_kind": action.surface_kind.value,
            "intent_desc": action.intent_desc,
            "employee_id": action.payload.get("employee_id", ""),
            "task": action.payload.get("task", ""),
        }
        return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _directory_employee_ids(self) -> set[str] | None:
        """Fail-soft read of the Fase-3 directory's employee_id set.

        None means "no directory pushed" (visibility_scope="all", the
        default) — callers must skip the UX pre-check entirely then (zero
        regression). This is a faster/clearer LOCAL signal only: the cloud
        already enforces the department gate authoritatively (404 on the
        outbox POST) regardless of what this returns.
        """
        try:
            assoc = self._store.get()
            directory = getattr(assoc, "directory", None) if assoc else None
            if not isinstance(directory, dict):
                return None
            entries = directory.get("entries", [])
            if not isinstance(entries, list):
                return None
            return {e.get("employee_id", "") for e in entries if isinstance(e, dict)}
        except Exception:  # noqa: BLE001
            return None

    def _resolve_conversation_id(self, work_item_id: Any) -> str | None:
        """Best-effort: reads `agent_tasks.conversation_id` for the CURRENT
        task. None for an autonomous (non-chat) task — non-fatal."""
        if work_item_id is None:
            return None
        try:
            conn = sqlite3.connect(str(self._db_path))
            row = conn.execute(
                "SELECT conversation_id FROM agent_tasks WHERE task_id = ?",
                (str(work_item_id),),
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            return None
        return row[0] if row and row[0] else None

    async def _post_outbox(self, *, employee_id: str, task: str) -> dict[str, Any]:
        """POST {cloud}/v1/outbox. Never raises — returns {"ok": False,
        "error": ...} on ANY transport/association failure."""
        try:
            assoc = self._store.get()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"association_store_error: {exc}"}
        if assoc is None or not self._store.is_associated():
            return {"ok": False, "error": "instance_not_associated"}
        if not _endpoint_is_safe(assoc.cloud_endpoint):
            return {"ok": False, "error": "cloud_endpoint_unsafe"}

        instance_secret = self._store.reveal_instance_secret()
        if not instance_secret:
            return {"ok": False, "error": "instance_secret_unavailable"}

        correlation_id = str(uuid4())
        body = {
            "to_employee_id": employee_id,
            "to_agent_id": "",
            "body": task,
            "correlation_id": correlation_id,
        }
        try:
            # asyncio.to_thread: httpx.post is blocking I/O — replay() is an
            # async Protocol method invoked from the daemon's busy broker
            # dispatch loop (unlike config_sync's own sequential tick), so a
            # synchronous call here would stall every other in-flight task.
            resp = await asyncio.to_thread(
                httpx.post,
                f"{assoc.cloud_endpoint.rstrip('/')}/v1/outbox",
                headers={"Authorization": f"Bearer {instance_secret}"},
                json=body,
                timeout=self._timeout_s,
                follow_redirects=False,  # SSRF mitigation — mirrors config_sync's calls.
            )
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"outbox_transport_error: {exc}"}
        if resp.status_code not in (200, 201, 204):
            return {"ok": False, "error": f"outbox_http_error: {resp.status_code}"}
        return {"ok": True, "correlation_id": correlation_id}


def _validate_params(
    params: dict[str, Any], visible_employee_ids: set[str] | None = None
) -> tuple[str, str, str]:
    """Returns (employee_id, task, error) — error is "" when valid.

    visible_employee_ids: the Fase-3 directory's employee_id set, when a
    directory is present for this instance. None means no directory was
    pushed (visibility_scope="all", the default) — skip the check entirely
    (zero regression, opt-in). This is a UX PRE-CHECK ONLY: the cloud
    enforces the department gate authoritatively regardless of this result.
    """
    employee_id = str(params.get("employee_id") or "").strip()
    task = str(params.get("task") or "").strip()
    if not employee_id or not task:
        return "", "", (
            "delegate_to_colleague requiere 'employee_id' y 'task' no vacíos"
        )
    if visible_employee_ids is not None and employee_id not in visible_employee_ids:
        return "", "", "Ese compañero no está en tu directorio de departamento."
    return employee_id, task, ""


def _record_correlation(
    *, db_path: Path, correlation_id: str, conversation_id: str
) -> None:
    """Delegates to `config_sync.delegation_inbox` — single owner of the
    `delegation_outbox_correlations` bookkeeping table. Best-effort: a
    failure here never fails the delegation itself (the message was already
    delivered)."""
    try:
        from hermes.config_sync.delegation_inbox import (  # noqa: PLC0415
            record_delegation_correlation,
        )

        record_delegation_correlation(
            db_path=db_path, correlation_id=correlation_id,
            conversation_id=conversation_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.delegation_surface_adapter.record_correlation_failed",
            extra={"correlation_id": correlation_id, "reason": str(exc)},
        )


def _endpoint_is_safe(endpoint: str) -> bool:
    """HTTPS-only / no-credentials-in-url / not-a-private-IP — same guard
    config_sync/remote_approvals apply before trusting `cloud_endpoint`."""
    try:
        _validate_cloud_endpoint(endpoint)
        return True
    except PairingError as exc:
        logger.error(
            "hermes.delegation_surface_adapter.endpoint_unsafe",
            extra={"reason": str(exc)},
        )
        return False
