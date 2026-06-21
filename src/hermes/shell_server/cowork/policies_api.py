"""Security/Policies API — the backing of the per-command Policies UI (P4.B).

  GET  /api/v1/policies              → {preset, tools:{name:enabled}, overridden:[...]}
  POST /api/v1/policies/preset       body: {preset, totp, riddle_answer}
  POST /api/v1/policies/tool         body: {tool, enabled, totp, riddle_answer}

The owner sees EVERY command and toggles it (checkboxes) or picks a preset
(Equilibrado / Permisivo / Bloqueado). A disabled command is refused at the universal
tool gate (security_hook). Changing the policy is itself a MOST-DELICATE action —
weakening your own defenses — so every mutation requires MFA + the human riddle. This
stops the agent (or an injection) from quietly opening its own cage: it cannot mint the
TOTP nor answer the riddle (no access to the owner-only secret).

Read is open (the UI renders the current state); mutations are MFA-gated.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes.capabilities.tool_policy import Preset, ToolPolicyStore
from hermes.shell_server.security.mfa import MfaStore, ProtectionLevel

logger = logging.getLogger("hermes.shell_server.cowork.policies_api")


class PresetBody(BaseModel):
    preset: Literal["equilibrado", "permisivo", "bloqueado"]
    totp: str
    riddle_answer: str | None = None


class ToolBody(BaseModel):
    tool: str
    enabled: bool
    totp: str
    riddle_answer: str | None = None


class MfaOnDangersBody(BaseModel):
    enabled: bool
    totp: str
    riddle_answer: str | None = None


def create_policies_router(
    policy: ToolPolicyStore | None = None, mfa: MfaStore | None = None
) -> APIRouter:
    router = APIRouter()
    store = policy or ToolPolicyStore()
    mfa_store = mfa or MfaStore()

    @router.get("/api/v1/policies")
    async def get_policies() -> dict:
        return store.snapshot()

    @router.post("/api/v1/policies/preset")
    async def set_preset(body: PresetBody) -> dict:
        _require_owner_mfa(mfa_store, body.totp, body.riddle_answer)
        store.apply_preset(Preset(body.preset))
        logger.info("hermes.cowork.policies.preset_applied preset=%s", body.preset)
        return {"ok": True, "preset": body.preset}

    @router.post("/api/v1/policies/tool")
    async def set_tool(body: ToolBody) -> dict:
        _require_owner_mfa(mfa_store, body.totp, body.riddle_answer)
        store.set_tool(body.tool, body.enabled)
        logger.info(
            "hermes.cowork.policies.tool_set tool=%s enabled=%s", body.tool, body.enabled
        )
        return {"ok": True, "tool": body.tool, "enabled": body.enabled}

    @router.post("/api/v1/policies/mfa_on_dangers")
    async def set_mfa_on_dangers(body: MfaOnDangersBody) -> dict:
        # The escape hatch: turning MFA-on-dangers OFF makes cage-escaping dangers run
        # autonomously (owner-responsible). DISABLING the danger gate is the agent's
        # self-widening vector → gated MFA + riddle, which the caged agent cannot mint.
        _require_owner_mfa(mfa_store, body.totp, body.riddle_answer)
        store.set_mfa_on_dangers(body.enabled)
        logger.info("hermes.cowork.policies.mfa_on_dangers_set enabled=%s", body.enabled)
        return {"ok": True, "mfa_on_dangers": body.enabled}

    return router


def _require_owner_mfa(mfa_store: MfaStore, totp: str, riddle_answer: str | None) -> None:
    """Changing the security policy is most-delicate → MFA + riddle. Fail-closed."""
    if not mfa_store.is_enrolled():
        raise HTTPException(status_code=403, detail={
            "code": "mfa_not_enrolled",
            "message": "Configura el MFA antes de cambiar las políticas de seguridad."})
    ok, reason = mfa_store.verify(
        level=ProtectionLevel.MFA_RIDDLE, totp=totp or "", riddle_answer=riddle_answer)
    if not ok:
        logger.warning("hermes.cowork.policies.mfa_denied reason=%s", reason)
        raise HTTPException(status_code=401, detail={
            "code": reason,
            "message": "Cambiar las políticas exige tu código MFA y la respuesta del acertijo."})
