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

Runtime-only, INERT in this phase: `hermes.runtime.security_hook` currently
consults `route()` for observability only and treats ENTERPRISE identically to
LOCAL (falls through to the existing local native-danger approval path). The
remote-approver consult (posting to a cloud approver, awaiting its resolution)
is a LATER phase.

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
    if tenant_gate and eligible:
        return ApprovalRoute.ENTERPRISE
    return ApprovalRoute.LOCAL
