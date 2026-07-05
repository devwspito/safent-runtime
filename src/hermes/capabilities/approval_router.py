"""Enterprise approval routing — Fase 2 Phase 4a.

Pure function: given a tool's already-computed delicacy/sensitivity/irreversible
signals plus the calling agent's cloud-management + tenant configuration, decide
WHERE the human-in-the-loop approval for THIS action must be resolved.

HARDBLOCK is NEVER produced by `route()`. The hardline / self-jailbreak /
denylist floor (security_hook.py Steps 2/3/6) runs BEFORE any approvable action
ever reaches this router, and is NEVER routed anywhere — it stays
deny-for-everyone, including Enterprise-managed agents. `route()` is only ever
consulted for actions that already passed that floor (i.e. actions that ARE
approvable — the only open question is BY WHOM).

LIVE as of Fase 2 Phase 4b: `hermes.runtime.security_hook` (`_compute_danger_
route`) consults `route()` to decide who actually resolves a native-danger
approval. ENTERPRISE diverts the approval to a remote Enterprise approver —
the row is registered with `route="enterprise"` and can ONLY be resolved by a
verified, signed decision applied via `hermes.config_sync.remote_approvals`
(the local owner can still DENY it directly, never approve it locally — see
that module's I-1/I-2/I-3). LOCAL keeps today's D-Bus approve/deny path
unchanged. `route()` itself remains pure/inert: it never blocks/allows and
never substitutes the floor above — it only ever runs for an action that was
ALREADY going to require (and receive) an approval seam.

Pure, no I/O, zero framework deps.
"""

from __future__ import annotations

from enum import StrEnum

from hermes.capabilities.tool_delicacy import Delicacy
from hermes.capabilities.tool_sensitivity import SensitivityCategory

_CLOUD_MANAGED = "cloud"


class ApprovalRoute(StrEnum):
    """Where a human-in-the-loop approval for THIS action must be resolved."""

    LOCAL = "local"            # the local human, gated by the agent's own permissions (today)
    ENTERPRISE = "enterprise"  # a remote Enterprise approver (routing signal only in Phase 4a)
    HARDBLOCK = "hardblock"    # NEVER produced by route() — see module docstring


def route(
    *,
    tool: str,  # noqa: ARG001 — kept for call-site symmetry/observability; unused in the decision
    delicacy: Delicacy,
    sensitivity_categories: frozenset[SensitivityCategory],
    irreversible: bool,
    agent_managed_by: str | None,
    tenant_remote_approval_enabled: bool,
    approval_tier: str = "coordinator",
) -> ApprovalRoute:
    """Decide LOCAL vs ENTERPRISE for one already-approvable tool call.

    Truth table — ENTERPRISE requires BOTH the tenant gate AND an eligibility
    trigger; anything else falls to LOCAL (today's behaviour, unchanged):

      tenant_gate = agent_managed_by == "cloud" AND tenant_remote_approval_enabled
      eligible    = delicacy is MOST_DELICATE OR sensitivity_categories OR irreversible

      tenant_gate | eligible | route
      ------------|----------|------------
      False       | False    | LOCAL
      False       | True     | LOCAL       (no remote approver configured for this tenant/agent)
      True        | False    | LOCAL       (nothing Enterprise-worthy about this action)
      True        | True     | ENTERPRISE
    """
    tenant_gate = agent_managed_by == _CLOUD_MANAGED and tenant_remote_approval_enabled
    eligible = (
        delicacy is Delicacy.MOST_DELICATE
        or bool(sensitivity_categories)
        or irreversible
    )
    # Per-role tier (restrict-only): a COORDINATOR agent is trusted to self-resolve
    # DELICATE actions at the LOCAL owner gate (base behaviour). A STANDARD agent
    # (or any non-"coordinator"/unknown tier — fail-closed) additionally escalates
    # DELICATE to a remote ENTERPRISE approver, so an employee cannot self-approve a
    # DELICATE action their coordinator is meant to sign off. This can ONLY flip
    # LOCAL→ENTERPRISE, only when a remote approver exists (tenant_gate), and never
    # touches the kernel floor. Default "coordinator" preserves today's behaviour for
    # any caller that does not pass a tier.
    escalate_delicate = approval_tier != "coordinator" and delicacy is Delicacy.DELICATE
    if tenant_gate and (eligible or escalate_delicate):
        return ApprovalRoute.ENTERPRISE
    return ApprovalRoute.LOCAL
