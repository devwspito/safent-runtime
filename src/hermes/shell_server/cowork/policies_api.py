"""Security/Policies API — the backing of the per-command Policies UI (P4.B).

  GET  /api/v1/policies              → {preset, tools:{name:enabled}, overridden:[...]}
  POST /api/v1/policies/preset       body: {preset, totp}
  POST /api/v1/policies/tool         body: {tool, enabled, totp}
  POST /api/v1/policies/tools        body: {tools:{name:enabled}, totp}   (batch save)

The owner sees EVERY command and toggles it (checkboxes) or picks a preset
(Equilibrado / Permisivo / Bloqueado). A disabled command is refused at the universal
tool gate (security_hook). Changing the policy weakens your own defenses, so every
mutation requires the owner's TOTP (TOTP-only model, owner decision 2026-06-24). This
stops the agent (or an injection) from quietly opening its own cage: it cannot mint the
TOTP (no access to the owner-only 0600 secret).

Read is open (the UI renders the current state); mutations are MFA-gated.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from hermes.capabilities.tool_policy import Preset, ToolPolicyStore
from hermes.shell_server.security.mfa import MfaStore
from hermes.shell_server.security.owner_mfa_gate import require_owner_mfa

logger = logging.getLogger("hermes.shell_server.cowork.policies_api")


class PresetBody(BaseModel):
    preset: Literal["equilibrado", "permisivo", "bloqueado"]
    totp: str


class ToolBody(BaseModel):
    tool: str
    enabled: bool
    totp: str


class ToolsBody(BaseModel):
    """Batch tool toggle — the owner edits checkboxes and saves once (one TOTP)."""

    tools: dict[str, bool]
    totp: str


class MfaOnDangersBody(BaseModel):
    enabled: bool
    totp: str


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
        require_owner_mfa(mfa_store, body.totp, action="cambiar las políticas de seguridad")
        store.apply_preset(Preset(body.preset))
        # The browser egress plane follows the preset: PERMISIVO opens the netns-isolated
        # browser to the open web (open-logged) so research actually works; Equilibrado/
        # Bloqueado keep default-deny + the owner's explicit grants. Best-effort — the
        # preset still applies if the proxy push fails.
        try:
            from hermes.shell_server.egress_api import apply_browser_egress_for_preset  # noqa: PLC0415
            apply_browser_egress_for_preset()
        except Exception:  # noqa: BLE001
            logger.warning("hermes.cowork.policies.egress_apply_failed", exc_info=True)
        logger.info("hermes.cowork.policies.preset_applied preset=%s", body.preset)
        return {"ok": True, "preset": body.preset}

    @router.post("/api/v1/policies/tool")
    async def set_tool(body: ToolBody) -> dict:
        require_owner_mfa(mfa_store, body.totp, action="cambiar las políticas de seguridad")
        store.set_tool(body.tool, body.enabled)
        logger.info(
            "hermes.cowork.policies.tool_set tool=%s enabled=%s", body.tool, body.enabled
        )
        return {"ok": True, "tool": body.tool, "enabled": body.enabled}

    @router.post("/api/v1/policies/tools")
    async def set_tools(body: ToolsBody) -> dict:
        # Batch: the owner edits many checkboxes locally and saves once → ONE MFA prompt
        # for the whole change set (not one per toggle).
        require_owner_mfa(mfa_store, body.totp, action="cambiar las políticas de seguridad")
        for tool, enabled in body.tools.items():
            store.set_tool(tool, enabled)
        logger.info("hermes.cowork.policies.tools_set count=%d", len(body.tools))
        return {"ok": True, "count": len(body.tools)}

    @router.post("/api/v1/policies/mfa_on_dangers")
    async def set_mfa_on_dangers(body: MfaOnDangersBody) -> dict:
        # The escape hatch: turning MFA-on-dangers OFF makes cage-escaping dangers run
        # autonomously (owner-responsible). DISABLING the danger gate is the agent's
        # self-widening vector → gated on the owner's TOTP, which the caged agent cannot mint.
        require_owner_mfa(mfa_store, body.totp, action="cambiar las políticas de seguridad")
        store.set_mfa_on_dangers(body.enabled)
        logger.info("hermes.cowork.policies.mfa_on_dangers_set enabled=%s", body.enabled)
        return {"ok": True, "mfa_on_dangers": body.enabled}

    return router

