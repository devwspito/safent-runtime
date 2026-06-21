"""Security center REST API — D-Bus surface for scans, policy, and audit head.

Endpoints:
  GET  /api/v1/security/scans         list recent security scans
  GET  /api/v1/security/policy        get current security policy
  GET  /api/v1/security/audit/head    get audit chain head (integrity status)
  POST /api/v1/security/scans/install run a pre-install security scan
  POST /api/v1/security/decisions     record an install decision (approve/deny)

Security:
  - All reads are fail-soft (return degraded state, not 503).
  - Scan + decision are mutators; fail-hard 503 (CTRL-P1-11).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes.shell_server.security.mfa import MfaStore, ProtectionLevel
from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes.shell_server.cowork.security_api")

# Decisions that ELEVATE an install past a FAIL/WARN scan verdict (sovereign owner
# override, modelo "todo elevable"). These are MOST-DELICATE → require owner MFA+riddle,
# same bar as changing a security policy. A plain "deny"/"block" needs no MFA.
_OVERRIDE_DECISIONS = frozenset({"allow", "approve", "allowed", "allow_once", "install", "installed"})


def _require_owner_mfa(mfa_store: MfaStore, totp: str, riddle_answer: str | None) -> None:
    """Verify owner MFA+riddle for a sovereign install override. 401 on failure."""
    if not mfa_store.is_enrolled():
        raise HTTPException(status_code=403, detail={"code": "mfa_not_enrolled",
            "message": "Configura el MFA antes de permitir una instalación que el "
            "antivirus marcó (es una acción soberana del dueño)."})
    # Eleva a MFA_RIDDLE (lo más delicado, igual que cambiar una política) SI el dueño
    # tiene acertijo configurado; si solo tiene TOTP, exige al menos el TOTP. Nunca
    # menos que MFA para una acción que salta el veredicto del antivirus.
    level = ProtectionLevel.MFA_RIDDLE if mfa_store.state().riddle_question else ProtectionLevel.MFA
    ok, reason = mfa_store.verify(level=level, totp=totp or "", riddle_answer=riddle_answer)
    if not ok:
        raise HTTPException(status_code=401, detail={"code": reason,
            "message": "Permitir un paquete con veredicto FAIL exige tu código MFA"
            + (" y la respuesta del acertijo" if level == ProtectionLevel.MFA_RIDDLE else "")
            + ". Quedará auditado."})


# ------------------------------------------------------------------
# Pydantic schemas
# ------------------------------------------------------------------


class ScanInstallRequest(BaseModel):
    kind: str = Field(min_length=1, description="Target kind: skill, mcp, flatpak, rpm")
    identifier: str = Field(min_length=1, description="Package/skill identifier to scan")


class RecordInstallDecisionRequest(BaseModel):
    scan_id: str = Field(min_length=1)
    decision: str = Field(min_length=1, description="approve/allow or deny")
    identifier: str = ""
    kind: str = ""
    score: int = -1
    verdict: str = ""
    risks_json: str = "[]"
    totp: str | None = None          # required to ALLOW a FAIL/WARN scan (owner MFA)
    riddle_answer: str | None = None


# ------------------------------------------------------------------
# Router factory
# ------------------------------------------------------------------


def create_security_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/security", tags=["security"])

    @router.get("/scans")
    async def list_recent_scans(request: Request, limit: int = 50) -> list[dict]:
        """List recent security scans.

        Fail-soft: returns [] when daemon unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_list("list_recent_scans", limit)
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.security.scans.unavailable",
                extra={"reason": str(exc)},
            )
            return []

    @router.get("/policy")
    async def get_security_policy(request: Request) -> dict:
        """Get the current security policy.

        Fail-soft: returns empty dict when daemon unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_dict("get_security_policy")
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.security.policy.unavailable",
                extra={"reason": str(exc)},
            )
            return {}

    @router.get("/audit/head")
    async def get_audit_chain_head(request: Request) -> dict:
        """Get the audit chain head (integrity status and head hash).

        Fail-soft: returns degraded status when daemon unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_dict("get_audit_chain_head")
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.security.audit_head.unavailable",
                extra={"reason": str(exc)},
            )
            return {"integrity": "unknown", "head_hash": ""}

    @router.post("/scans/install", status_code=202)
    async def scan_install(request: Request, body: ScanInstallRequest) -> dict:
        """Run a pre-install security scan.

        Returns {scan_id, verdict, score, risks}. fail-hard on daemon unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_mutator("scan_install", body.kind, body.identifier)
        except AgentUnavailable as exc:
            _raise_503(exc, "scan_install")

    @router.post("/decisions", status_code=201)
    async def record_install_decision(
        request: Request, body: RecordInstallDecisionRequest
    ) -> dict:
        """Record an operator decision on a security scan (approve/allow or deny).

        ALLOW/APPROVE on a non-PASS scan is a SOVEREIGN override → requires owner
        MFA+riddle (audited). Plain deny needs none. fail-hard on daemon unavailable.
        """
        if body.decision.strip().lower() in _OVERRIDE_DECISIONS:
            _require_owner_mfa(MfaStore(), body.totp, body.riddle_answer)
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_mutator(
                "record_install_decision",
                body.scan_id,
                body.decision,
                body.identifier,
                body.kind,
                body.score,
                body.verdict,
                body.risks_json,
            )
        except AgentUnavailable as exc:
            _raise_503(exc, "record_install_decision")

    return router


def _raise_503(exc: AgentUnavailable, operation: str) -> None:
    logger.warning(
        "hermes.security.mutator_unavailable",
        extra={"operation": operation, "reason": str(exc)},
    )
    raise HTTPException(
        status_code=503,
        detail={
            "code": "agent_unavailable",
            "message": "El agente no está disponible. Comprueba que hermes-runtime está activo.",
        },
    ) from exc
