"""Retire packages without deleting identities/history or replacing execution."""

import asyncio
import inspect
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from hermes.agents.domain.agent import Agent, AgentDraft, default_agent
from hermes.agents.domain.ports import CannotDeleteDefaultAgent
from hermes.agents.domain.retired_factory import RETIRED_FACTORY_IDS, FactoryAgentRetired
from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry
from hermes.shell_server.cowork.agents_api import create_agents_router
from hermes.shell_server.cowork.roster_api import _build_departments

pytestmark = pytest.mark.unit
FACTORY_ID = "roster-ventas-prospector"


def test_every_edition_seeds_only_native_default_and_has_no_enable_switch(tmp_path):
    path = tmp_path / "state.db"
    for _ in range(3):
        registry = SqliteAgentRegistry(db_path=path)
        assert [a.agent_id for a in registry.list_agents()] == ["default"]
    assert "seed_default_roster" not in inspect.signature(SqliteAgentRegistry).parameters
    assert not hasattr(registry, "set_default_roster_enabled")
    with registry._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='agent_settings'").fetchone()
            is None
        )
    with pytest.raises(CannotDeleteDefaultAgent):
        registry.delete_agent("default")


def test_legacy_factory_rows_are_preserved_but_cannot_run_or_reappear(tmp_path):
    path = tmp_path / "state.db"
    registry = SqliteAgentRegistry(db_path=path)
    with registry._connect() as conn:
        for identity in RETIRED_FACTORY_IDS:
            registry._insert(
                conn, Agent(agent_id=identity, name="historical", instructions="preserve")
            )
        before = [tuple(r) for r in conn.execute("SELECT * FROM agents ORDER BY agent_id")]
    registry = SqliteAgentRegistry(db_path=path)
    assert [a.agent_id for a in registry.list_agents()] == ["default"]
    for identity in RETIRED_FACTORY_IDS:
        with pytest.raises(FactoryAgentRetired):
            registry.get_agent(identity)
        with pytest.raises(FactoryAgentRetired):
            registry.persona_for(identity)
        with pytest.raises(FactoryAgentRetired):
            registry.create_agent(AgentDraft(name="revive", agent_id=identity, managed_by="cloud"))
    with registry._connect() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM agents ORDER BY agent_id")] == before


def test_unknown_prefix_custom_and_existing_cloud_profiles_are_preserved(tmp_path):
    registry = SqliteAgentRegistry(db_path=tmp_path / "state.db")
    custom = registry.create_agent(AgentDraft(name="custom", agent_id="roster-customer-own"))
    with registry._connect() as conn:
        registry._insert(
            conn, Agent(agent_id=FACTORY_ID, name="assigned company profile", managed_by="cloud")
        )
    assert {a.agent_id for a in registry.list_agents()} == {"default", custom.agent_id, FACTORY_ID}
    assert registry.persona_for(custom.agent_id).name == "custom"
    assert registry.persona_for(FACTORY_ID).name == "assigned company profile"


def test_old_factory_only_database_restores_native_default_without_erasing_rows(tmp_path):
    path = tmp_path / "state.db"
    registry = SqliteAgentRegistry(db_path=path)
    with registry._connect() as conn:
        registry._insert(conn, Agent(agent_id=FACTORY_ID, name="old"))
        conn.execute("DELETE FROM agents WHERE agent_id='default'")
    reopened = SqliteAgentRegistry(db_path=path)
    assert [a.agent_id for a in reopened.list_agents()] == ["default"]
    with reopened._connect() as conn:
        assert (
            conn.execute("SELECT name FROM agents WHERE agent_id=?", (FACTORY_ID,)).fetchone()[0]
            == "old"
        )


def test_absent_factory_identity_cannot_fall_back_to_default(tmp_path):
    registry = SqliteAgentRegistry(db_path=tmp_path / "state.db")
    with pytest.raises(FactoryAgentRetired):
        registry.persona_for(FACTORY_ID)
    assert registry.persona_for("ordinary-missing").name == default_agent().name


def test_removed_rest_dbus_switches_are_not_exported():
    from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import Runtime1ServiceInterface

    assert all("default-roster" not in route.path for route in create_agents_router().routes)
    assert not hasattr(Runtime1ServiceInterface, "GetDefaultRosterEnabled")
    assert not hasattr(Runtime1ServiceInterface, "SetDefaultRosterEnabled")


def test_custom_department_is_never_classified_as_factory():
    departments = _build_departments(
        [{"agent_id": "custom", "name": "Own", "department": "ventas"}]
    )
    assert departments[0]["kind"] == "custom"
    assert departments[0]["agents"][0]["source"] == "custom"


@pytest.mark.parametrize("broken_registry", [False, True])
def test_engine_does_not_swallow_retirement_into_persona_fallback(tmp_path, broken_registry):
    from hermes.runtime.nous_engine import NousReasoningEngine

    engine = object.__new__(NousReasoningEngine)
    engine._persona = default_agent().to_persona()
    engine._agent_registry = (
        Mock(persona_for=Mock(side_effect=RuntimeError("broken")))
        if broken_registry
        else SqliteAgentRegistry(db_path=tmp_path / "state.db")
    )
    with pytest.raises(FactoryAgentRetired):
        engine._resolve_cycle_persona(FACTORY_ID)


def test_default_persona_uses_native_delegation_without_packaged_claims():
    rules = " ".join(default_agent().golden_rules)
    assert "delegación nativa" in rules
    assert "especialistas YA listos" not in rules
    assert "Office" not in rules
    assert "no amplía permisos" in rules


async def test_native_delegation_keeps_real_identity_not_prompt_guessed_specialist(monkeypatch):
    from hermes.runtime import live_activity
    from hermes.runtime.nous_engine import _build_tool_call_emitter

    record, edge = Mock(), Mock()
    monkeypatch.setattr(live_activity, "record", record)
    monkeypatch.setattr(live_activity, "record_delegation", edge)
    sink = Mock(emit=AsyncMock())
    task_id = uuid4()
    accumulator = []
    emitter = _build_tool_call_emitter(
        sink, task_id, asyncio.get_running_loop(), accumulator, live_agent_id="default"
    )
    await asyncio.to_thread(
        emitter,
        "delegate_task",
        {
            "role": "ventas prospector marketing legal",
            "goal": "research safely",
        },
    )
    record.assert_called_once_with(str(task_id), "default", "delegate_task")
    edge.assert_not_called()
    sink.emit.assert_awaited_once()
    assert len(accumulator) == 1
