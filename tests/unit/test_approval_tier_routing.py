"""Per-role approval tier routing (2026-07-05).

A COORDINATOR agent self-resolves DELICATE actions at the LOCAL owner gate; a
STANDARD agent escalates them to a remote ENTERPRISE approver. Restrict-only:
the tier can only flip LOCAL→ENTERPRISE, only when a remote approver exists
(tenant gate), and never touches the kernel floor.
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
from hermes.capabilities.tool_delicacy import Delicacy

_FZ = frozenset()


def _route(tier: str, delicacy: Delicacy, *, cloud=True, remote=True):
    return route(
        tool="x", delicacy=delicacy, sensitivity_categories=_FZ, irreversible=False,
        agent_managed_by="cloud" if cloud else None,
        tenant_remote_approval_enabled=remote, approval_tier=tier,
    )


def test_coordinator_self_resolves_delicate_locally():
    assert _route("coordinator", Delicacy.DELICATE) is ApprovalRoute.LOCAL


def test_standard_escalates_delicate_to_enterprise():
    assert _route("standard", Delicacy.DELICATE) is ApprovalRoute.ENTERPRISE


def test_most_delicate_escalates_for_both_tiers():
    assert _route("coordinator", Delicacy.MOST_DELICATE) is ApprovalRoute.ENTERPRISE
    assert _route("standard", Delicacy.MOST_DELICATE) is ApprovalRoute.ENTERPRISE


def test_normal_stays_local_for_both():
    assert _route("coordinator", Delicacy.NORMAL) is ApprovalRoute.LOCAL
    assert _route("standard", Delicacy.NORMAL) is ApprovalRoute.LOCAL


def test_standard_delicate_without_remote_approver_stays_local():
    # No remote approver configured → cannot escalate; owner gate (LOCAL) still fires.
    assert _route("standard", Delicacy.DELICATE, remote=False) is ApprovalRoute.LOCAL
    assert _route("standard", Delicacy.DELICATE, cloud=False) is ApprovalRoute.LOCAL


def test_unknown_tier_fails_closed_like_standard():
    # Fail-closed: any non-"coordinator" tier escalates DELICATE (max gating).
    assert _route("weird", Delicacy.DELICATE) is ApprovalRoute.ENTERPRISE


def test_default_tier_preserves_pre_feature_behaviour():
    # A caller that omits approval_tier gets today's base routing (back-compat).
    r = route(
        tool="x", delicacy=Delicacy.DELICATE, sensitivity_categories=_FZ,
        irreversible=False, agent_managed_by="cloud", tenant_remote_approval_enabled=True,
    )
    assert r is ApprovalRoute.LOCAL


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
