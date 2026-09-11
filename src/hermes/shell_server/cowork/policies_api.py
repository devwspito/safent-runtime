"""Security/Policies API — the backing of the per-command Policies UI (P4.B).

  GET  /api/v1/policies              → {preset, tools:{name:enabled}, overridden:[...]}
  POST /api/v1/policies/preset       body: {preset, totp}
  POST /api/v1/policies/tool         body: {tool, enabled, totp}
  POST /api/v1/policies/tools        body: {tools:{name:enabled}, totp}   (batch save)

The owner sees EVERY command and toggles it (checkboxes) or picks a preset
(Equilibrado / Permisivo / Bloqueado). A disabled command is refused at the universal
tool gate (security_hook). Changing the policy weakens your own defenses, so mutations
require the owner's TOTP (TOTP-only model, owner decision 2026-06-24) — EXCEPT while
the owner has explicitly turned `mfa_on_dangers` off, in which case preset/tool/tools
(non-sovereign decisions) go through with no TOTP, matching the posture the owner
already chose. The `mfa_on_dangers` toggle itself is SOVEREIGN: it always requires
TOTP when MFA is enrolled, regardless of its current value — otherwise the owner could
never re-enable it from the UI once off (specs/025-safent-repaso SEG-15). Either way
the caged agent cannot mint the TOTP (no access to the owner-only 0600 secret).

Read is open (the UI renders the current state); mutations are MFA-gated per the rule
above.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from hermes.capabilities.tool_policy import Preset, ToolPolicyStore
from hermes.shell_server.security.mfa import MfaStore
from hermes.shell_server.security.owner_mfa_gate import (
    require_owner_mfa,
    require_owner_mfa_if_enrolled,
)

logger = logging.getLogger("hermes.shell_server.cowork.policies_api")


class PresetBody(BaseModel):
    preset: Literal["equilibrado", "permisivo", "bloqueado"]
    totp: str = ""


class ToolBody(BaseModel):
    tool: str
    enabled: bool
    totp: str = ""


class ToolsBody(BaseModel):
    """Batch tool toggle — the owner edits checkboxes and saves once (one TOTP)."""

    tools: dict[str, bool]
    totp: str = ""


class MfaOnDangersBody(BaseModel):
    enabled: bool
    totp: str = ""


def create_policies_router(
    policy: ToolPolicyStore | None = None, mfa: MfaStore | None = None
) -> APIRouter:
    router = APIRouter()
    store = policy or ToolPolicyStore()

    @router.get("/api/v1/policies")
    async def get_policies() -> dict:
        return store.snapshot()

    @router.post("/api/v1/policies/preset")
    async def set_preset(body: PresetBody) -> dict:
        # Non-sovereign decision: gated on the CURRENT mfa_on_dangers posture —
        # SEG-15, see set_mfa_on_dangers below for the sovereign toggle itself.
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
        store.set_tool(body.tool, body.enabled)
        logger.info(
            "hermes.cowork.policies.tool_set tool=%s enabled=%s", body.tool, body.enabled
        )
        return {"ok": True, "tool": body.tool, "enabled": body.enabled}

    @router.post("/api/v1/policies/tools")
    async def set_tools(body: ToolsBody) -> dict:
        # Batch: the owner edits many checkboxes locally and saves once → ONE MFA prompt
        # for the whole change set (not one per toggle) — only while mfa_on_dangers is on.
        for tool, enabled in body.tools.items():
            store.set_tool(tool, enabled)
        logger.info("hermes.cowork.policies.tools_set count=%d", len(body.tools))
        return {"ok": True, "count": len(body.tools)}

    @router.post("/api/v1/policies/mfa_on_dangers")
    async def set_mfa_on_dangers(body: MfaOnDangersBody) -> dict:
        # SOVEREIGN switch: gated on its OWN state (enrolled → TOTP required,
        # both ON and OFF), never on its current value — a plain
        # require_owner_mfa here made re-enabling it, once off, a 401
        # dead-end (SEG-15). Turning MFA-on-dangers OFF makes cage-escaping
        # dangers run autonomously (owner-responsible); the caged agent still
        # cannot mint the owner's TOTP either way.
        store.set_mfa_on_dangers(body.enabled)
        logger.info("hermes.cowork.policies.mfa_on_dangers_set enabled=%s", body.enabled)
        return {"ok": True, "mfa_on_dangers": body.enabled}

    return router
