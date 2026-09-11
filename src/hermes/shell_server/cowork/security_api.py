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

Owner-approval flow (Part 2 contract):
  1. Frontend calls POST /api/v1/security/scans/install with {kind, identifier}.
     Response shape:
       {
         scan_id: str,
         verdict: "PASS" | "WARN" | "FAIL",
         score: int (0–100),
         engine: "trivy" | "heuristic",
         engine_label: str,               # human-readable provenance
         requires_owner_approval: bool,   # true when verdict != "PASS"
         risks: [{category, severity, message, evidence_ref}]
       }
  2. If requires_owner_approval=true, show the risk list and an approval dialog.
  3. To approve, POST /api/v1/security/decisions with:
       {
         scan_id: str,        # from step 1
         decision: "approve", # or "allow" / "allow_once" / "installed"
       }
     Response: { ok: true, approval_grant?: str } after a recorded decision.
     Overrides require the owner's UI session, not the internal daemon bearer.
  4. After a successful decision the ScanService gate records decision=ALLOWED
     so the install verb (add_mcp_server / install_hub_skill / install_package)
     sees the override in cache and proceeds.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from hermes.shell_server.security.owner_confirmation import (
    issue_owner_approval,
    require_owner_session,
)
from hermes.tasks.control_plane.domain.ports import AgentUnavailable
from hermes.tasks.domain.ports import AgentPauseProvenance

logger = logging.getLogger("hermes.shell_server.cowork.security_api")

# Decisions that ELEVATE an install past a FAIL/WARN scan verdict (sovereign owner
# override, modelo "todo elevable"). These require the owner's UI session.
# The internal daemon bearer cannot elevate an installation on its own.
_OVERRIDE_DECISIONS = frozenset(
    {"allow", "approve", "allowed", "allow_once", "install", "installed"}
)


# ------------------------------------------------------------------
# Pydantic schemas
# ------------------------------------------------------------------


class ScanInstallRequest(BaseModel):
    kind: str = Field(min_length=1, description="Target kind: skill, mcp, flatpak, rpm")
    identifier: str = Field(min_length=1, description="Package/skill identifier to scan")


class RecordInstallDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scan_id: str = Field(min_length=1)
    decision: str = Field(min_length=1, description="approve/allow or deny")
    identifier: str = ""
    kind: str = ""
    score: int = -1
    verdict: str = ""
    risks_json: str = "[]"


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engaged: bool = Field(description="True = engage the brake, False = release it")
    reason: str = Field(default="", max_length=500, description="Owner's reason, audited")


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

        Returns:
          {
            scan_id: str,
            verdict: "PASS" | "WARN" | "FAIL",
            score: int,
            engine: "trivy" | "heuristic",
            engine_label: str,             # human-readable: what type of scan ran
            requires_owner_approval: bool, # true when verdict != "PASS"
            risks: [{category, severity, message, evidence_ref}]
          }

        When engine="heuristic", the scan ran without a full CVE database and the
        verdict may be conservative.  If requires_owner_approval=true, the owner
        can approve via POST /api/v1/security/decisions.

        Fail-hard 503 on daemon unavailable.
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

        ALLOW/APPROVE requires the authenticated owner UI and a recorded decision.
        Fail hard on daemon failure; never issue a grant after a rejected write.
        """
        is_override = body.decision.strip().lower() in _OVERRIDE_DECISIONS
        if is_override:
            require_owner_session(request)
        proxy = request.app.state.dbus_proxy
        try:
            result = await proxy.call_mutator(
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

        if not isinstance(result, dict) or result.get("ok") is not True:
            raise HTTPException(status_code=502, detail={"code": "decision_not_recorded"})

        # Only a successfully recorded owner decision authorizes the follow-up.
        if is_override and body.kind == "skill" and body.identifier:
            grant = issue_owner_approval(
                request, identifier=body.identifier, action="install_hub_skill"
            )
            result = {**result, "approval_grant": grant}
        return result

    @router.get("/kill-switch")
    async def get_kill_switch(request: Request) -> dict:
        """Estado del freno de emergencia. Fail-soft: {engaged: false, ...} si
        el daemon no está disponible (nunca 503 en una lectura de estado)."""
        proxy = request.app.state.dbus_proxy
        try:
            status = await proxy.call_dict("get_kill_switch_status")
        except AgentUnavailable:
            return {"engaged": False, "reason": None, "changed_by": None, "changed_at": None}
        return status or {"engaged": False, "reason": None, "changed_by": None, "changed_at": None}

    @router.post("/kill-switch", status_code=200)
    async def set_kill_switch(request: Request, body: KillSwitchRequest) -> dict:
        """Engage/release the emergency brake.

        Engaging is a one-click brake behind the normal operator authentication.
        Releasing requires the owner's UI session, never the internal daemon
        bearer. Community does not ask for TOTP or a device password here.
        """
        proxy = request.app.state.dbus_proxy
        if body.engaged:
            try:
                await proxy.call_bool("pause", body.reason)
            except AgentUnavailable as exc:
                _raise_503(exc, "kill_switch_engage")
            return {"ok": True, "engaged": True}

        require_owner_session(request)
        release_reason = AgentPauseProvenance.API

        try:
            # Audit provenance (security review 2026-09-10, MEDIUM finding):
            # distinguishes this owner-session UI/REST release from `safent
            # brake release`'s "host_cli" on the signed AGENT_RESUMED entry.
            await proxy.call_bool("resume", release_reason)
        except AgentUnavailable as exc:
            _raise_503(exc, "kill_switch_release")
        return {"ok": True, "engaged": False}

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
