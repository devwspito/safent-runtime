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

ESCALATED MFA MODEL (owner decision 2026-06-25):
Per-action HITL approvals use a tiered model:
  - simple tier: most tools → Aprobar/Rechazar without MFA.
  - mfa tier: cage-widening (MOST_DELICATE) + destructive/irreversible tools → TOTP required.
`is_mfa_required(tool)` is the single query point for ALL surfaces (gate, web API, frontend).
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
    "cronjob",
})

# Orquestación INTERNA: delegar / repartir trabajo entre agentes del roster NO es un peligro
# en sí mismo — el peligro es lo que el sub-agente LUEGO hace, y ESO lo gatea la jaula (kernel:
# Landlock/netns/seccomp) + el broker en CADA tool del sub-agente. Por eso la delegación es
# NORMAL (fluida, sin pedir aprobación): el control real es la jaula, y nunca un estorbo.
# El sub-agente corre bajo el mismo daemon, misma jaula y mismo broker que el Cerebro.
# (Decisión del dueño 2026-06-23: "tú no controlas la delegación; controlas a nivel de kernel
# si un agente intenta un comando peligroso".)
_ORCHESTRATION: frozenset[str] = frozenset({"delegate_task", "mixture_of_agents"})


def delicacy(tool: str) -> Delicacy:
    """Classify a tool's owner-approval delicacy.

    DELICATE is DERIVED PURELY from the native nous_tool_risk_map (any WRITE tool) — no
    re-listed/invented tool names here, so it can never drift from the native source of
    truth. The only hand-list is the _MOST_DELICATE governance overlay (non-native
    capability/conceptual actions). Unknown tools → NORMAL (the cage confines them
    regardless of this tier)."""
    if tool in _ORCHESTRATION:
        return Delicacy.NORMAL  # delegación fluida; la jaula gatea lo que el sub-agente HACE
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


# ---------------------------------------------------------------------------
# Escalated MFA model — per-action HITL tier (owner decision 2026-06-25).
#
# TWO AXES — do NOT conflate them:
#
#   _MOST_DELICATE  (security-hook / delicacy() axis)
#     → Tools blocked by the pre-dispatch hook; governs default-off and riddle MFA.
#     → Includes "cronjob" because scheduling a cron IS cage-widening (capability
#       changes that survive across sessions).
#
#   _MFA_TIER_HITL  (per-action HITL approval axis)
#     → Tools whose HITL *approval card* requires TOTP before the owner can allow.
#     → "cronjob" is SIMPLE here (owner decision 2026-06-25: schedule delegation is
#       approved with a plain click; the cage still enforces at execution time).
#     → Set = (_MOST_DELICATE - scheduling tools) ∪ _DESTRUCTIVE.
#
# One edit to each set re-aligns the whole stack. No per-file lists to drift.
#
# _DESTRUCTIVE — tools whose PRIMARY registered contract is PERMANENT DATA LOSS.
# NOT a word-list scan of command content (feedback_no_deterministic_routing).
# Native exec tools (terminal/execute_code) that MAY run rm are handled by Nous's
# own _await_gateway_decision; the cage native gate covers them separately.
_DESTRUCTIVE: frozenset[str] = frozenset({
    # Future: "delete_file", "drop_table", "purge_records"
})


def is_destructive(tool: str) -> bool:
    """True if *tool*'s PRIMARY registered contract is PERMANENT DATA LOSS.

    Single query point over the curated `_DESTRUCTIVE` overlay above — consumed
    by approval_router.route()'s `irreversible` input (Enterprise governance,
    Fase 2 Phase 4a). Currently empty (see _DESTRUCTIVE docstring); destructive
    native tools land here as they're added to the catalog. No re-listing: one
    edit to `_DESTRUCTIVE` re-aligns both is_mfa_required() and this query.
    """
    return tool in _DESTRUCTIVE


# Tools in _MOST_DELICATE that are SIMPLE tier for per-action HITL approvals.
# The cage + broker enforcement still applies; TOTP is not required to approve.
_MOST_DELICATE_SIMPLE_HITL: frozenset[str] = frozenset({"cronjob"})

# Per-action HITL mfa tier = cage-widening tools (minus scheduling) + destructive.
_MFA_TIER_HITL: frozenset[str] = (
    _MOST_DELICATE - _MOST_DELICATE_SIMPLE_HITL
) | _DESTRUCTIVE


def is_mfa_required(tool: str) -> bool:
    """True when a per-action HITL approval requires owner TOTP.

    Escalated MFA model (owner decision 2026-06-25):
      - _MFA_TIER_HITL tools (install_* / set_policy / disable_mfa / skill_manage) → MFA.
      - _DESTRUCTIVE tools (irreversible data loss) → MFA.
      - cronjob and everything else → simple (no MFA, plain Approve/Deny button).

    Single source of truth consumed by:
      - sqlite_approval_gate.approve() (enforcement point)
      - approvals_api._to_frontend() (required_level field for the frontend)
      - ApprovalCard.tsx (conditional MfaModal)
    """
    return tool in _MFA_TIER_HITL


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
