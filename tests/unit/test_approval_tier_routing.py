"""Per-role approval tier routing — NOW INERT (Fase 2 Phase 4c, 2026-07-06).

The TOTP-keyed model routes purely on `tool_delicacy.is_mfa_required(tool)`:
the worker has no TOTP (centralized at Enterprise), so an MFA-tier action on
a cloud-managed, remote-approval-enabled tenant ALWAYS escalates to
ENTERPRISE — a COORDINATOR and a STANDARD agent route the SAME tool
identically. `approval_tier` no longer flips LOCAL<->ENTERPRISE; it is kept
only for back-compat/observability (see approval_router.route()'s docstring).
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from hermes.capabilities.approval_router import ApprovalRoute, route
from hermes.capabilities.domain.agent_access_scope import AgentAccessScope
from hermes.capabilities.infrastructure.schema import ensure_capabilities_schema
from hermes.capabilities.infrastructure.sqlite_agent_access_scope_repo import (
    SqliteAgentAccessScopeRepo,
)

# skill_manage: MOST_DELICATE and MFA-tier (is_mfa_required == True).
_MFA_TOOL = "skill_manage"
# send_message: DELICATE (native WRITE) but NOT MFA-tier — the owner's
# report-review example ("my boss asks my agent for a report — I still
# review it before it sends").
_SIMPLE_TOOL = "send_message"


def _route(tier: str, tool: str, *, cloud=True, remote=True):
    return route(
        tool=tool,
        agent_managed_by="cloud" if cloud else None,
        tenant_remote_approval_enabled=remote,
        approval_tier=tier,
    )


def test_coordinator_and_standard_route_simple_tool_identically_local():
    assert _route("coordinator", _SIMPLE_TOOL) is ApprovalRoute.LOCAL
    assert _route("standard", _SIMPLE_TOOL) is ApprovalRoute.LOCAL


def test_coordinator_and_standard_route_mfa_tool_identically_enterprise():
    assert _route("coordinator", _MFA_TOOL) is ApprovalRoute.ENTERPRISE
    assert _route("standard", _MFA_TOOL) is ApprovalRoute.ENTERPRISE


def test_mfa_tool_without_remote_approver_stays_local():
    # No remote approver configured → cannot escalate; worker gate (LOCAL) still fires.
    assert _route("standard", _MFA_TOOL, remote=False) is ApprovalRoute.LOCAL
    assert _route("standard", _MFA_TOOL, cloud=False) is ApprovalRoute.LOCAL


def test_unknown_tier_does_not_affect_routing():
    # approval_tier is inert — any value routes identically to "standard"/"coordinator".
    assert _route("weird", _MFA_TOOL) is ApprovalRoute.ENTERPRISE
    assert _route("weird", _SIMPLE_TOOL) is ApprovalRoute.LOCAL


def test_default_tier_preserves_totp_keyed_behaviour():
    # A caller that omits approval_tier still routes on is_mfa_required alone.
    r = route(
        tool=_MFA_TOOL, agent_managed_by="cloud", tenant_remote_approval_enabled=True,
    )
    assert r is ApprovalRoute.ENTERPRISE


def _fresh_repo() -> SqliteAgentAccessScopeRepo:
    db = Path(tempfile.mkdtemp()) / "s.db"
    conn = sqlite3.connect(db)
    ensure_capabilities_schema(conn)
    conn.commit()
    conn.close()
    return SqliteAgentAccessScopeRepo(db_path=db)


def test_scope_round_trip_preserves_approval_tier():
    repo = _fresh_repo()
    scope = AgentAccessScope.create(
        tenant_id="t1", agent_id="a1", updated_by=886,
        native_tools=frozenset(["read_file"]), enforced=True, managed_by="cloud",
        approval_tier="coordinator",
    )
    repo.upsert(scope)
    got = repo.get_scope("a1", "t1")
    assert got is not None
    assert got.approval_tier == "coordinator"
    assert "approval_tier" in scope.to_dict()


def test_scope_default_tier_is_standard():
    repo = _fresh_repo()
    scope = AgentAccessScope.create(tenant_id="t1", agent_id="a2", updated_by=886)
    repo.upsert(scope)
    assert repo.get_scope("a2", "t1").approval_tier == "standard"


def test_parse_access_scope_json_accepts_and_validates_tier():
    from hermes.agents_os.infrastructure.dbus_runtime_service import (
        _parse_access_scope_json,
    )
    import json

    ok, err = _parse_access_scope_json(json.dumps({"enforced": True, "approval_tier": "coordinator"}))
    assert err is None and ok["approval_tier"] == "coordinator"
    # absent → standard (drop-when-standard wire shape)
    ok2, err2 = _parse_access_scope_json(json.dumps({"enforced": True}))
    assert err2 is None and ok2["approval_tier"] == "standard"
    # invalid tier → rejected (fail-closed at the trust boundary)
    _, err3 = _parse_access_scope_json(json.dumps({"approval_tier": "root"}))
    assert err3 is not None
