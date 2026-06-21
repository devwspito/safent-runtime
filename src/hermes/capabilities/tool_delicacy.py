"""THE single source of truth for tool delicacy across ALL governance layers.

Before this, three layers each had their own list (the MFA approval tier, the policy
default-on/off, the security hook), and they drifted — a tool could be "most delicate"
to approve yet "on by default" in the policy. That is exactly the inter-layer conflict
we must not have. Now every layer derives from `delicacy(tool)` here:

  - approvals_api  → MFA tier (normal=TOTP, delicate=+humanity, most=+riddle)
  - tool_policy    → Equilibrado default (most-delicate is OFF; owner opts in)
  - (the cage routing — which tools run in the sandbox — is an ORTHOGONAL axis in
     nous_engine._CAGED_NATIVE_TOOLS: ALL exec/file tools are confined regardless of
     delicacy; delicacy only governs owner-approval friction + the default toggle.)

One edit here re-aligns the whole stack. No per-file lists to drift.
"""

from __future__ import annotations

from enum import StrEnum

from hermes.runtime.nous_tool_risk_map import NousRisk, classify_nous_tool


class Delicacy(StrEnum):
    NORMAL = "normal"                # owner approval = MFA;            default ON
    DELICATE = "delicate"            # owner approval = MFA + humanity; default ON (confined)
    MOST_DELICATE = "most_delicate"  # owner approval = MFA + riddle;   default OFF (opt-in)


# REUSE, DON'T DUPLICATE: Hermes already classifies every NATIVE tool READ/WRITE in
# nous_tool_risk_map (the native catalog). DELICATE is DERIVED from it (any native WRITE)
# — we do NOT re-list native tools here (that drift was the inter-layer conflict). The
# ONLY hand-maintained sets are the two governance OVERLAYS below, which are coarser
# than the native READ/WRITE axis:
#
#   _MOST_DELICATE — weakens own defenses / installs / schedules / spawns. These are
#     CAPABILITY + conceptual tools (install_*, set_policy, disable_mfa) that the native
#     Nous risk map does not cover (it only classifies native Nous tools). NOT a
#     duplication of native names — a governance overlay over non-native actions.
#     Approval = MFA + riddle; default OFF.
_MOST_DELICATE: frozenset[str] = frozenset({
    "set_agent_permission_rule", "clear_agent_permission_rule", "set_policy",
    "set_security_policy", "disable_mfa", "update_system_setting", "change_system_setting",
    "install_app", "install_mcp", "install_skill", "skill_manage",
    "cronjob", "delegate_task", "mixture_of_agents",
})


def delicacy(tool: str) -> Delicacy:
    """Classify a tool's owner-approval delicacy.

    DELICATE is DERIVED PURELY from the native nous_tool_risk_map (any WRITE tool) — no
    re-listed/invented tool names here, so it can never drift from the native source of
    truth. The only hand-list is the _MOST_DELICATE governance overlay (non-native
    capability/conceptual actions). Unknown tools → NORMAL (the cage confines them
    regardless of this tier)."""
    if tool in _MOST_DELICATE:
        return Delicacy.MOST_DELICATE
    if classify_nous_tool(tool) is NousRisk.WRITE:
        return Delicacy.DELICATE
    return Delicacy.NORMAL


def default_enabled_equilibrado(tool: str) -> bool:
    """Equilibrado preset default: everything ON except most-delicate (owner opts in)."""
    return delicacy(tool) is not Delicacy.MOST_DELICATE


# Cage-CONTAINED writes: DELICATE, but their blast radius is fully inside the sandbox
# (uid 999, isolated fs) — the cage already confines them, so they do NOT need owner MFA
# even when mfa_on_dangers is ON. Everything else DELICATE/MOST_DELICATE ESCAPES the cage
# in effect (arbitrary exec, outbound network, install, governance) and DOES require MFA.
# (Owner decision 2026-06-19: "MFA on what escapes the cage" — keeps autonomy fluid for
# the contained writes/reads, asks only where owner confirmation actually matters.)
_CAGE_CONTAINED: frozenset[str] = frozenset({"write_file", "patch"})


# SINGLE SOURCE for the exec/file tools that route through the OpenShell cage. Lives in
# this light, neutral module (no heavy nous_engine import) so BOTH the cage routing
# (nous_engine) and the Policies catalog (tool_policy) consume the same set — no
# hand-listed duplicate. Names match the native nous_tool_risk_map EXACTLY (no invented
# aliases: an unclassified name falls to default-deny, not the cage).
CAGED_NATIVE_EXEC_TOOLS: frozenset[str] = frozenset({"terminal", "execute_code", "process"})
CAGED_NATIVE_FILE_TOOLS: frozenset[str] = frozenset({"read_file", "search_files", "write_file", "patch"})
CAGED_NATIVE_TOOLS: frozenset[str] = CAGED_NATIVE_EXEC_TOOLS | CAGED_NATIVE_FILE_TOOLS


def hook_mfa_block(tool: str, *, mfa_on_dangers: bool) -> bool:
    """Whether the security_hook must block this tool pending owner MFA.

    SCOPED TO NATIVE Nous tools only — capability/external tools (install_app, Composio,
    MCP) bypass the hook and are gated by the BROKER's own per-action HITL (web UI + MFA);
    blocking them here would dead-end their approval flow. Coherence audit 2026-06-19.

    Within native tools (which bypass the broker → the hook IS their gate):
      - MOST_DELICATE (skill_manage / cronjob / delegate_task — self-modify, schedule,
        spawn): ALWAYS blocks. The escape hatch (mfa_on_dangers=OFF) NEVER frees the
        actions the agent would use to widen itself — not even in full-autonomy mode.
      - cage-ESCAPING DELICATE (send_message / discord / ha_call_service — outbound):
        blocks only when mfa_on_dangers is ON; the owner's escape hatch frees them OFF.
      - caged-exec (gateway pause-cards them), cage-contained writes (write_file/patch),
        and reads: never block here (the cage / gateway handle them).
    """
    if classify_nous_tool(tool) is None:
        return False  # not a native tool → broker gates it; don't dead-end its approval
    if tool in CAGED_NATIVE_EXEC_TOOLS or tool in _CAGE_CONTAINED:
        return False
    d = delicacy(tool)
    if d is Delicacy.MOST_DELICATE:
        return True  # self-widening: MFA always, escape hatch does not apply
    if d is Delicacy.DELICATE:
        return mfa_on_dangers  # cage-escaping outbound: MFA while the owner keeps the gate up
    return False  # NORMAL
