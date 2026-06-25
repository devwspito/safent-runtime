"""HITL approvals for the Lumen Cowork web UI (P4 elevation gate).

  GET  /api/v1/approvals/pending          → [{proposal_id, kind, summary, target, required_level}]
  POST /api/v1/approvals/{proposal_id}     body: {decision, totp?}
  GET  /api/v1/mfa/status                  → {enrolled}
  POST /api/v1/mfa/enroll                  body: {totp?}  → {otpauth_uri, secret}

Escalated MFA model (owner decision 2026-06-25):
  - simple tier  (most tools: cronjob, send_message, delegate_task …)
      → Approve/Deny without MFA.  required_level="simple".
  - mfa tier (MOST_DELICATE: install_*/set_policy/disable_mfa/skill_manage +
      destructive/irreversible tools)
      → TOTP required.  required_level="mfa".

Classification is from tool_delicacy.is_mfa_required (single source of truth).
The agent is isolated by netns — it cannot call this endpoint (bearer + network
isolation). For simple-tier proposals the human pressing Approve IS proof of presence.
For mfa-tier the TOTP adds the one factor the caged agent cannot reach (owner-only
0600 secret).

Policy changes (policies_api.py: set_preset/set_policy_tools/set_mfa_on_dangers)
still require MFA — those endpoints are NOT touched here.
"""

from __future__ import annotations

import logging
import os
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hermes.capabilities.infrastructure.sqlite_approval_gate import ApprovalGateError
from hermes.capabilities.proposal_summary import human_summary
from hermes.capabilities.tool_delicacy import is_mfa_required
from hermes.shell_server.security.mfa import MfaStore, ProtectionLevel
from hermes.shell_server.security.mfa_tool_tier import MfaFactors
from hermes.tasks.control_plane.domain.ports import AgentUnavailable, AuthenticatedChannel

logger = logging.getLogger("hermes.shell_server.cowork.approvals_api")

# Tier labels used in the required_level field (consumed by ApprovalCard.tsx).
_LEVEL_SIMPLE = "simple"
_LEVEL_MFA = ProtectionLevel.MFA.value  # "mfa"


class ApprovalDecision(BaseModel):
    decision: Literal["once", "always", "deny"]
    totp: str | None = None  # required only for mfa-tier proposals; ignored for simple-tier


class EnrollBody(BaseModel):
    totp: str | None = None  # required only to ROTATE an existing enrollment


class RiddleBody(BaseModel):
    totp: str
    question: str
    answer: str


def create_approvals_router(mfa: MfaStore | None = None) -> APIRouter:
    router = APIRouter()
    store = mfa or MfaStore()

    @router.get("/api/v1/approvals/pending")
    async def list_pending_approvals(request: Request) -> list[dict]:
        try:
            rows = await request.app.state.control_plane.list_hitl_pending()
        except (AgentUnavailable, Exception):  # noqa: BLE001
            logger.warning("hermes.cowork.approvals.list_unavailable")
            return []
        return [_to_frontend(r, store) for r in rows]

    @router.get("/api/v1/mfa/status")
    async def mfa_status() -> dict:
        st = store.state()
        return {"enrolled": st.enrolled}

    @router.post("/api/v1/mfa/enroll")
    async def mfa_enroll(body: EnrollBody) -> dict:
        # First enrollment is open (bootstrap). Re-enrolling (rotating) requires the
        # CURRENT code, so a compromised caller can't silently swap the secret.
        if store.is_enrolled():
            ok, reason = store.verify(level=ProtectionLevel.MFA, totp=body.totp or "")
            if not ok:
                raise HTTPException(status_code=401, detail={"code": reason,
                    "message": "Para rotar el MFA, introduce el código actual."})
        uri, secret = store.enroll()
        logger.info("hermes.cowork.mfa.enrolled rotated=%s", store.is_enrolled())
        return {"otpauth_uri": uri, "secret": secret}

    @router.post("/api/v1/mfa/riddle")
    async def mfa_set_riddle(body: RiddleBody) -> dict:
        ok, reason = store.verify(level=ProtectionLevel.MFA, totp=body.totp)
        if not ok:
            raise HTTPException(status_code=401, detail={"code": reason,
                "message": "Código MFA inválido."})
        store.set_riddle(body.question, body.answer)
        return {"ok": True}

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

        # APPROVE — ALWAYS forward the owner's TOTP if provided. The GATE is the single
        # MFA enforcement point: it decides FROM THE STORED tool_name whether MFA is
        # required and verifies it. (Bug fixed 2026-06-25: this used to re-fetch the
        # pending list and STRING-MATCH the proposal_id to decide whether to forward the
        # TOTP. When that match missed — e.g. id-format/timing — an mfa-tier approval was
        # sent with mfa_factors=None and the gate DENIED a VALID approval with mfa_required.
        # The owner "entered the right TOTP" and it was dropped before the gate. We now
        # forward whatever the owner signed and let the gate be the source of truth + return
        # the precise reason [mfa_required|invalid_totp|mfa_not_enrolled] for a friendly msg.)
        mfa_factors: MfaFactors | None = (
            MfaFactors(totp=body.totp) if (body.totp and body.totp.strip()) else None
        )

        try:
            raw = await request.app.state.control_plane.approve(
                channel=channel, proposal_id=parsed_id,
                mfa_factors=mfa_factors)
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
                "hermes.cowork.approvals.approved proposal=%s totp=%s live=%s",
                proposal_id, "yes" if mfa_factors else "no", live,
            )
            return {"ok": True, "decision": body.decision, "live": live}
        except ApprovalGateError as exc:
            gate_reason = getattr(exc, "reason", "approval_failed")
            status = 401 if gate_reason in {"mfa_required", "invalid_totp",
                                             "mfa_not_enrolled"} else 400
            raise HTTPException(status_code=status, detail={"code": gate_reason,
                "message": _mfa_reason_message(gate_reason)}) from exc
        except AgentUnavailable as exc:
            raise _unavailable(proposal_id, exc) from exc

    return router


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_MFA_REASON_MESSAGES: dict[str, str] = {
    "mfa_not_enrolled": "No hay MFA configurado. Configúralo antes de aprobar acciones.",
    "invalid_totp": "Código TOTP incorrecto o expirado.",
    "mfa_required": "Se requiere MFA para aprobar esta acción.",
    "proposal_invalid": "Esta aprobación ya no es válida (puede haber expirado o ya fue "
                        "resuelta). Refresca el panel.",
}


def _mfa_reason_message(reason: str) -> str:
    return _MFA_REASON_MESSAGES.get(
        reason,
        "Verificación MFA fallida: código o respuesta del acertijo incorrectos, "
        "o falta un factor para esta acción.",
    )


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


def _to_frontend(row: dict, store: MfaStore) -> dict:
    tool_name = row.get("tool_name", "")
    risk = row.get("risk", "")
    mfa_state = store.state()
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
        # Escalated MFA model: mfa-tier tools require TOTP; simple-tier do not.
        "required_level": _LEVEL_MFA if is_mfa_required(tool_name) else _LEVEL_SIMPLE,
        "mfa_enrolled": mfa_state.enrolled,
    }
