"""Community HITL decisions through the authenticated owner channel.

The shell's auth/network boundary protects these routes. Enterprise-routed
proposals still require a signed cloud decision; local denial remains possible.
MFA belongs to Enterprise and is not a Community approval factor.
"""

from __future__ import annotations

import logging
import os
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from hermes.capabilities.infrastructure.sqlite_approval_gate import ApprovalGateError
from hermes.capabilities.proposal_summary import human_summary
from hermes.tasks.control_plane.domain.ports import AgentUnavailable, AuthenticatedChannel

logger = logging.getLogger("hermes.shell_server.cowork.approvals_api")

class ApprovalDecision(BaseModel):
    # This gate consumes a single proposal; it does not install standing rules.
    model_config = ConfigDict(extra="forbid")
    decision: Literal["once", "deny"]


def create_approvals_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/approvals/pending")
    async def list_pending_approvals(request: Request) -> list[dict]:
        try:
            rows = await request.app.state.control_plane.list_hitl_pending()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.cowork.approvals.list_unavailable")
            # An unavailable gate is not an empty queue. Polling clients retain
            # their last known pending requests on 503 rather than erasing them.
            raise HTTPException(status_code=503, detail={
                "code": "approvals_unavailable",
                "message": "No se pueden consultar las aprobaciones en este momento.",
            }) from exc
        return [_to_frontend(r) for r in rows]

    @router.post("/api/v1/approvals/{proposal_id}", status_code=200)
    async def resolve_approval(request: Request, proposal_id: str, body: ApprovalDecision) -> dict:
        parsed_id = _parse_proposal_id(proposal_id)
        channel = AuthenticatedChannel(sender_uid=os.getuid())

        if body.decision == "deny":
            try:
                await request.app.state.control_plane.reject(
                    channel=channel, proposal_id=parsed_id,
                    reason="denied by operator via web UI")
                return {"ok": True, "decision": "deny"}
            except AgentUnavailable as exc:
                raise _unavailable(proposal_id, exc) from exc

        try:
            raw = await request.app.state.control_plane.approve(
                channel=channel, proposal_id=parsed_id)
            # raw is a JSON string from the D-Bus adapter: {"token": ..., "live": bool}
            # live=True  → LIVE: the blocked conversation thread was signalled; the
            #              exact tool call is executing right now.
            # live=False → POST: no thread was waiting (timed out / turn already ended);
            #              the tool did NOT execute; owner must ask the agent again.
            # For non-D-Bus adapters (tests / future adapters) raw may be a plain
            # string or None — default to live=True to avoid false "expired" messages
            # on paths that don't track threading.
            live: bool = True
            if isinstance(raw, str):
                import json as _json  # noqa: PLC0415
                try:
                    parsed = _json.loads(raw)
                    if isinstance(parsed, dict) and "live" in parsed:
                        live = bool(parsed["live"])
                except (ValueError, TypeError):
                    pass  # non-JSON string → keep live=True default
            logger.info(
                "hermes.cowork.approvals.approved proposal=%s live=%s",
                proposal_id, live,
            )
            return {"ok": True, "decision": body.decision, "live": live}
        except ApprovalGateError as exc:
            gate_reason = getattr(exc, "reason", "approval_failed")
            status = _status_for_gate_reason(gate_reason)
            raise HTTPException(status_code=status, detail={"code": gate_reason,
                "message": _approval_reason_message(gate_reason)}) from exc
        except AgentUnavailable as exc:
            raise _unavailable(proposal_id, exc) from exc

    return router


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_APPROVAL_REASON_MESSAGES: dict[str, str] = {
    "proposal_invalid": "Esta aprobación ya no es válida (puede haber expirado o ya fue "
                        "resuelta). Refresca el panel.",
    # Fase 2 Phase 4b: this row is routed to Enterprise — only a signed cloud
    # decision can approve it. The owner can still Deny (I-2); Approve here is
    # rejected fail-closed by the gate (sqlite_approval_gate.approve()).
    "enterprise_route_requires_cloud_decision": (
        "Esta acción está pendiente de aprobación de tu empresa (Enterprise). "
        "No puedes aprobarla localmente, pero sí puedes rechazarla."
    ),
}

# Reasons that must surface as 403 Forbidden (the caller is not authorized to
# perform THIS specific resolution).
_FORBIDDEN_REASONS: frozenset[str] = frozenset({
    "enterprise_route_requires_cloud_decision",
})


def _approval_reason_message(reason: str) -> str:
    return _APPROVAL_REASON_MESSAGES.get(
        reason,
        "No se pudo resolver la aprobación. Actualiza el panel y revisa su estado.",
    )


def _status_for_gate_reason(reason: str) -> int:
    if reason in _FORBIDDEN_REASONS:
        return 403
    return 400


def _unavailable(proposal_id: str, exc: Exception) -> HTTPException:
    logger.warning("hermes.cowork.approvals.resolve_unavailable proposal=%s err=%s",
                   proposal_id, exc)
    return HTTPException(status_code=503, detail={"code": "agent_unavailable",
        "message": "El agente no está disponible. Comprueba que hermes-runtime está activo."})


def _parse_proposal_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_proposal_id",
            "message": f"Not a valid UUID: {raw!r}"}) from exc


def _to_frontend(row: dict) -> dict:
    tool_name = row.get("tool_name", "")
    risk = row.get("risk", "")
    parameters = row.get("parameters_redacted", {})
    return {
        "proposal_id": row.get("proposal_id", ""),
        "kind": risk,
        # Human-facing title built from the tool name — never raw technical justification.
        # Raw justification + parameters remain available as "technical_detail" for the
        # "Ver detalles técnicos" panel in the frontend.
        "summary": human_summary(tool_name, parameters),
        "technical_detail": row.get("justification", ""),
        "target": tool_name,
        # Show WHAT is being approved (redacted), not just the tool name (red-team
        # finding 5 transparency: the owner approves a specific action).
        "parameters": parameters,
        # C — chat anchor: the REAL chat conversation_id (None for pre-migration
        # rows or non-chat cycles like scheduled/autonomous tasks).
        "conversation_id": row.get("conversation_id") or None,
        # When the row was created (ISO). The frontend uses it to hide stale ghost
        # cards (older than the owner-wait window) that a timed-out thread may leave.
        "created_at": row.get("created_at") or None,
        "required_level": "simple",
        # Fase 2 Phase 4b: "enterprise" when only a signed cloud decision can
        # approve this row (Approve here fails with enterprise_route_requires_
        # cloud_decision — see resolve_approval); Deny always still works (I-2).
        "route": row.get("route") or "local",
    }
