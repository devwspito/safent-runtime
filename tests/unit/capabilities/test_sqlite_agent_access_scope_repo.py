"""Tests for SqliteAgentAccessScopeRepo — Enterprise Fase 2 Phase 1.

Covers: upsert/get_scope/list_by_agent round-trip, one-row-per-(tenant,agent)
upsert semantics, cross-tenant isolation, and idempotent DDL (init twice).
"""

from __future__ import annotations

from pathlib import Path

from hermes.capabilities.domain.agent_access_scope import AgentAccessScope
from hermes.capabilities.infrastructure.sqlite_agent_access_scope_repo import (
    SqliteAgentAccessScopeRepo,
)


def _scope(tenant_id="tenant-x", agent_id="agent-a", **overrides) -> AgentAccessScope:
    defaults = dict(tenant_id=tenant_id, agent_id=agent_id, updated_by=1001)
    defaults.update(overrides)
    return AgentAccessScope.create(**defaults)


class TestUpsertGetRoundtrip:
    def test_upsert_then_get_scope_roundtrip(self, tmp_path: Path) -> None:
        repo = SqliteAgentAccessScopeRepo(db_path=tmp_path / "shell-state.db")
        scope = _scope(
            native_tools=frozenset({"read_file", "terminal"}),
            policy_overlay={"terminal": {"note": "later phase"}},
            views=("dashboard", "billing"),
            enforced=True,
            cerebro_unrestricted=False,
            managed_by="cloud",
        )
        repo.upsert(scope)

        fetched = repo.get_scope("agent-a", "tenant-x")
        assert fetched is not None
        assert fetched.scope_id == scope.scope_id
        assert fetched.native_tools == frozenset({"read_file", "terminal"})
        assert fetched.policy_overlay == {"terminal": {"note": "later phase"}}
        assert fetched.views == ("dashboard", "billing")
        assert fetched.enforced is True
        assert fetched.cerebro_unrestricted is False
        assert fetched.managed_by == "cloud"
        assert fetched.updated_by == 1001

    def test_authorized_mcp_servers_roundtrip(self, tmp_path: Path) -> None:
        repo = SqliteAgentAccessScopeRepo(db_path=tmp_path / "shell-state.db")
        repo.upsert(_scope(authorized_mcp_servers=frozenset({"safent-control"})))

        fetched = repo.get_scope("agent-a", "tenant-x")
        assert fetched is not None
        assert fetched.authorized_mcp_servers == frozenset({"safent-control"})

    def test_upsert_replaces_authorized_mcp_servers(self, tmp_path: Path) -> None:
        repo = SqliteAgentAccessScopeRepo(db_path=tmp_path / "shell-state.db")
        repo.upsert(_scope(authorized_mcp_servers=frozenset({"old-mcp"})))
        repo.upsert(_scope(authorized_mcp_servers=frozenset({"safent-control"})))

        fetched = repo.get_scope("agent-a", "tenant-x")
        assert fetched.authorized_mcp_servers == frozenset({"safent-control"})

    def test_get_scope_returns_none_when_absent(self, tmp_path: Path) -> None:
        repo = SqliteAgentAccessScopeRepo(db_path=tmp_path / "shell-state.db")
        assert repo.get_scope("nope", "tenant-x") is None

    def test_upsert_replaces_single_row_per_tenant_agent(self, tmp_path: Path) -> None:
        """One row per (tenant_id, agent_id): a second upsert REPLACES, not adds."""
        repo = SqliteAgentAccessScopeRepo(db_path=tmp_path / "shell-state.db")
        repo.upsert(_scope(enforced=False, native_tools=frozenset({"read_file"})))
        repo.upsert(_scope(enforced=True, native_tools=frozenset({"write_file"})))

        fetched = repo.get_scope("agent-a", "tenant-x")
        assert fetched.enforced is True
        assert fetched.native_tools == frozenset({"write_file"})
        assert len(repo.list_by_agent("agent-a")) == 1

    def test_cross_tenant_isolation(self, tmp_path: Path) -> None:
        repo = SqliteAgentAccessScopeRepo(db_path=tmp_path / "shell-state.db")
        repo.upsert(_scope(tenant_id="tenant-a", enforced=True))
        repo.upsert(_scope(tenant_id="tenant-b", enforced=False))

        assert repo.get_scope("agent-a", "tenant-a").enforced is True
        assert repo.get_scope("agent-a", "tenant-b").enforced is False


class TestListByAgent:
    def test_list_by_agent_across_tenants(self, tmp_path: Path) -> None:
        repo = SqliteAgentAccessScopeRepo(db_path=tmp_path / "shell-state.db")
        repo.upsert(_scope(tenant_id="tenant-a"))
        repo.upsert(_scope(tenant_id="tenant-b"))
        repo.upsert(_scope(tenant_id="tenant-a", agent_id="agent-other"))

        scopes = repo.list_by_agent("agent-a")
        assert len(scopes) == 2
        assert {s.tenant_id for s in scopes} == {"tenant-a", "tenant-b"}

    def test_list_by_agent_empty_when_no_rows(self, tmp_path: Path) -> None:
        repo = SqliteAgentAccessScopeRepo(db_path=tmp_path / "shell-state.db")
        assert repo.list_by_agent("ghost") == []


class TestIdempotentDdl:
    def test_init_twice_does_not_raise(self, tmp_path: Path) -> None:
        db_path = tmp_path / "shell-state.db"
        repo1 = SqliteAgentAccessScopeRepo(db_path=db_path)
        repo1.upsert(_scope())

        # Re-initializing against the SAME db_path must be a no-op DDL-wise
        # and must not lose the row already written.
        repo2 = SqliteAgentAccessScopeRepo(db_path=db_path)
        assert repo2.get_scope("agent-a", "tenant-x") is not None
