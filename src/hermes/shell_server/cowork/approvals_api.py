"""HITL approvals + owner MFA for the Lumen Cowork web UI (P4 elevation gate).

  GET  /api/v1/approvals/pending          → [{proposal_id, kind, summary, target}]
  POST /api/v1/approvals/{proposal_id}     body: {decision, totp, humanity?, riddle_answer?}
  GET  /api/v1/mfa/status                  → {enrolled, riddle_set}
  POST /api/v1/mfa/enroll                  body: {totp?}  → {otpauth_uri}
  POST /api/v1/mfa/riddle                  body: {totp, question, answer}

Elevation contract (the deterministic gate): an action the agent cannot run is NOT a
dead end — it surfaces here as a pending proposal. The OWNER approves it. Approval
requires owner MFA so the AGENT cannot self-approve its own elevation (it has no access
to the TOTP secret — uid 999, sandboxed, no master.key, egress sealed). Delicacy tiers:
  - normal   → TOTP
  - delicate → TOTP + prove-humanity
  - most delicate (policy/security/install changes) → TOTP + enrolled human riddle

Approving requires MFA to be enrolled first (mandatory MFA). Denying never does —
rejecting is always safe. The operator-token middleware still fronts every mutator.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hermes.capabilities.infrastructure.sqlite_approval_gate import ApprovalGateError
from hermes.shell_server.security.mfa import MfaStore, ProtectionLevel
from hermes.shell_server.security.mfa_tool_tier import MfaFactors, classify_level
from hermes.tasks.control_plane.domain.ports import AgentUnavailable, AuthenticatedChannel

logger = logging.getLogger("hermes.shell_server.cowork.approvals_api")

# MFA verification now lives in the GATE (gate.approve), the single enforcement point
# for ALL surfaces (web + D-Bus) — red-team 2026-06-19, finding 3. This layer only
# forwards the owner's raw factors; it no longer classifies or verifies the tier
# (that drifted across layers). The tier logic is in shell_server.security.mfa_tool_tier.


class ApprovalDecision(BaseModel):
    decision: Literal["once", "always", "deny"]
    totp: str | None = None
    humanity: str | None = None
    riddle_answer: str | None = None


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
        return {"enrolled": st.enrolled, "riddle_set": st.riddle_question is not None,
                "riddle_question": st.riddle_question}

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

        # APPROVE → forward the owner's raw MFA factors to the GATE, the single
        # enforcement point. The agent (uid 999, no TOTP secret) cannot mint them.
        # Light is_enrolled pre-check only for a friendly 403; the gate re-verifies.
        if not store.is_enrolled():
            raise HTTPException(status_code=403, detail={"code": "mfa_not_enrolled",
                "message": "Configura el MFA antes de aprobar acciones (obligatorio)."})

        factors = MfaFactors(
            totp=body.totp, humanity=body.humanity, riddle_answer=body.riddle_answer)
        try:
            await request.app.state.control_plane.approve(
                channel=channel, proposal_id=parsed_id, mfa_factors=factors)
            logger.info("hermes.cowork.approvals.approved proposal=%s", proposal_id)
            return {"ok": True, "decision": body.decision}
        except ApprovalGateError as exc:
            reason = getattr(exc, "reason", "mfa_denied")
            logger.warning("hermes.cowork.approvals.mfa_denied proposal=%s reason=%s err=%s",
                           proposal_id, reason, exc)
            raise HTTPException(status_code=401, detail={"code": reason,
                "message": _mfa_reason_message(reason)}) from exc
        except AgentUnavailable as exc:
            raise _unavailable(proposal_id, exc) from exc

    return router


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_MFA_REASON_MESSAGES: dict[str, str] = {
    "mfa_not_enrolled": "No hay MFA configurado. Configúralo antes de aprobar acciones.",
    "invalid_totp": "Código TOTP incorrecto o expirado.",
    "humanity_required": "Esta acción requiere prueba de humanidad (math challenge).",
    "riddle_not_enrolled": "Esta acción requiere un acertijo personalizado. Configúralo en Seguridad.",
    "invalid_riddle": "Respuesta del acertijo incorrecta.",
    "mfa_required": "Se requiere MFA para aprobar esta acción.",
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
    return {
        "proposal_id": row.get("proposal_id", ""),
        "kind": risk,
        "summary": row.get("justification", ""),
        "target": tool_name,
        # Show WHAT is being approved (redacted), not just the tool name (red-team
        # finding 5 transparency: the owner approves a specific action).
        "parameters": row.get("parameters_redacted", {}),
        # C — chat anchor: the REAL chat conversation_id (None for pre-migration
        # rows or non-chat cycles like scheduled/autonomous tasks).
        "conversation_id": row.get("conversation_id") or None,
        # D — tier + enrollment state so the card can ask only what's required.
        "required_level": classify_level(risk, tool_name).value,
        "mfa_enrolled": mfa_state.enrolled,
        "riddle_set": mfa_state.riddle_question is not None,
    }
