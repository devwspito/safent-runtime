"""Enterprise approval routing — Fase 2 Phase 4c (TOTP-keyed model).

Pure function: given a tool's already-computed MFA/TOTP requirement plus the
calling agent's cloud-management + tenant configuration, decide WHERE the
human-in-the-loop approval for THIS action must be resolved.

HARDBLOCK is NEVER produced by `route()`. The hardline / self-jailbreak /
denylist floor (security_hook.py Steps 2/3/6) runs BEFORE any approvable action
ever reaches this router, and is NEVER routed anywhere — it stays
deny-for-everyone, including Enterprise-managed agents. `route()` is only ever
consulted for actions that already passed that floor (i.e. actions that ARE
approvable — the only open question is BY WHOM).

MODEL (owner decision 2026-07-06 — supersedes the delicacy/sensitivity/
irreversible eligibility calculus and the per-role `escalate_delicate` tier
from Fase 2 Phase 4a/4b):

  The worker — the local human operating a cloud-managed agent — has NO TOTP
  enrolled; TOTP is CENTRALIZED at Enterprise. Every per-action HITL approval
  is either SIMPLE (no TOTP) or MFA-tier (TOTP required). That split ALREADY
  EXISTS as the single source of truth `tool_delicacy.is_mfa_required(tool)`
  (also consulted by `SqliteApprovalGate.approve()` to gate the LOCAL mint):

    - SIMPLE  (is_mfa_required == False) -> the worker approves ALONE, LOCAL.
      Enterprise is never involved — the agent already has scope permission
      for these; the worker's card is a plain Approve/Deny (e.g. send_message:
      "my boss asks my agent for a report — I still review it before it
      sends").
    - MFA-TIER (is_mfa_required == True) -> the worker CANNOT approve (no
      TOTP) -> routes to ENTERPRISE, resolved ONLY by a signed decision from
      the tenant's centralized TOTP admin (`hermes.config_sync.
      remote_approvals`). The worker can still DENY it locally (I-2 in that
      module) — denial never requires TOTP.

      truth table:
        tenant_gate | is_mfa_required(tool) | route
        ------------|------------------------|------------
        False       | False                  | LOCAL
        False       | True                   | LOCAL   (Community: the single
                                                          local owner has their
                                                          OWN TOTP enrolled and
                                                          clears the MFA tier
                                                          locally via
                                                          SqliteApprovalGate.
                                                          approve() — unchanged)
        True        | False                  | LOCAL
        True        | True                   | ENTERPRISE

      tenant_gate = agent_managed_by == "cloud" AND tenant_remote_approval_enabled

  `_DESTRUCTIVE` (irreversible) tools are already unioned into
  `_MFA_TIER_HITL` inside tool_delicacy.py, so `is_mfa_required(tool)` alone
  is sufficient — no separate `irreversible`/`sensitivity_categories` input is
  needed here (avoids re-deriving a second, driftable eligibility calculus).

LIVE as of Fase 2 Phase 4b/4c: `hermes.runtime.security_hook`
(`_compute_danger_route`) consults `route()` to decide who actually resolves a
native-danger approval. ENTERPRISE diverts the approval to a remote Enterprise
approver — the row is registered with `route="enterprise"` and can ONLY be
resolved by a verified, signed decision applied via
`hermes.config_sync.remote_approvals` (the local owner can still DENY it
directly, never approve it locally — see that module's I-1/I-2/I-3). LOCAL
keeps today's D-Bus approve/deny path unchanged. `route()` itself remains
pure/inert: it never blocks/allows and never substitutes the floor above — it
only ever runs for an action that was ALREADY going to require (and receive)
an approval seam.

Pure, no I/O, zero framework deps.
"""

from __future__ import annotations

from enum import StrEnum

from hermes.capabilities.tool_delicacy import is_mfa_required

_CLOUD_MANAGED = "cloud"


class ApprovalRoute(StrEnum):
    """Where a human-in-the-loop approval for THIS action must be resolved."""

    LOCAL = "local"            # the local worker, no TOTP required
    ENTERPRISE = "enterprise"  # the tenant's centralized TOTP admin (remote)
    HARDBLOCK = "hardblock"    # NEVER produced by route() — see module docstring


def route(
    *,
    tool: str,
    agent_managed_by: str | None,
    tenant_remote_approval_enabled: bool,
    approval_tier: str = "coordinator",  # noqa: ARG001 — INERT, kept for back-compat/observability only (see below)
) -> ApprovalRoute:
    """Decide LOCAL vs ENTERPRISE for one already-approvable tool call.

    ENTERPRISE requires BOTH the tenant gate AND the tool's MFA/TOTP tier
    (`tool_delicacy.is_mfa_required`); anything else falls to LOCAL:

      tenant_gate = agent_managed_by == "cloud" AND tenant_remote_approval_enabled

      tenant_gate | is_mfa_required(tool) | route
      ------------|------------------------|------------
      False       | False                  | LOCAL
      False       | True                   | LOCAL       (Community: local owner has TOTP)
      True        | False                  | LOCAL       (simple tier — worker approves alone)
      True        | True                   | ENTERPRISE  (worker has no TOTP)

    `approval_tier` ("coordinator" | "standard") is accepted for call-site
    back-compat and observability ONLY — it no longer changes the routing
    decision. Under the TOTP-keyed model, the critical (MFA-tier) set is
    identical for every agent tier: a coordinator and a standard agent route
    the SAME tool to the SAME place, because the constraint is "does the
    worker hold a TOTP for this action", not "how trusted is this agent".
    """
    tenant_gate = agent_managed_by == _CLOUD_MANAGED and tenant_remote_approval_enabled
    if tenant_gate and is_mfa_required(tool):
        return ApprovalRoute.ENTERPRISE
    return ApprovalRoute.LOCAL
