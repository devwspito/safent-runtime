"""Real Textual widgets over an isolated registry; no D-Bus daemon simulation."""

from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable  # noqa: E402

from hermes.agents.application.serialization import agent_to_dict  # noqa: E402
from hermes.agents.domain.agent import Agent, AgentDraft, default_agent  # noqa: E402
from hermes.agents.domain.retired_factory import RETIRED_FACTORY_IDS  # noqa: E402
from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry  # noqa: E402
from hermes.runtime.nous_engine import NousReasoningEngine  # noqa: E402
from hermes.tui.app import SafentTerminal  # noqa: E402
from hermes.tui.bridge import OfflineRuntimeBridge, RuntimeBridge  # noqa: E402
from hermes.tui.modals.common import FormModal  # noqa: E402
from hermes.tui.screens.agents import AgentsPane  # noqa: E402
from hermes.tui.screens.chat import ChatPane  # noqa: E402
from hermes.tui.widgets.sidebar import NAV  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


async def test_profile_pane_lists_real_custom_profiles_without_global_activation(tmp_path):
    registry = SqliteAgentRegistry(db_path=tmp_path / "profiles.db")
    custom = registry.create_agent(AgentDraft(name="Mi perfil explícito"))
    with registry._connect() as conn:
        for identity in RETIRED_FACTORY_IDS:
            registry._insert(conn, Agent(agent_id=identity, name="Retired package"))

    class Bridge(OfflineRuntimeBridge):
        async def list_agents(self):
            return [agent_to_dict(agent) for agent in registry.list_agents()]

        async def call(self, member_snake, *args):
            assert member_snake not in {"get_active_agent", "set_active_agent"}
            return await super().call(member_snake, *args)

    app = SafentTerminal(bridge=Bridge())
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.go_to("agents")
        await pilot.pause()
        table = app.query_one("#agents-table", DataTable)
        assert table.row_count == 2
        assert set(table.rows) == {"default", custom.agent_id}
        assert app.query_one(AgentsPane).TITLE == "Perfiles"
        table.focus()
        await pilot.press("enter")  # no global activation mutation
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, FormModal)
        await pilot.press("escape")
        await pilot.pause()
        app.go_to("tasks")
        await pilot.pause()
        assert app.query_one("#content").current == "pane-tasks"


def test_sidebar_help_and_bridge_do_not_offer_global_active_agent():
    assert next(entry.label for entry in NAV if entry.pane_id == "agents") == "Perfiles"
    assert any(entry.pane_id == "tasks" and entry.label == "Tareas" for entry in NAV)
    assert not hasattr(RuntimeBridge, "get_active_agent")
    assert not hasattr(RuntimeBridge, "set_active_agent")
    assert not hasattr(AgentsPane, "action_activate_agent")


async def test_profile_slash_query_uses_current_list_without_active_global_lookup():
    pane = ChatPane()
    # Data handler has no mounted UI dependency; its bridge is supplied by app.
    from unittest.mock import PropertyMock, patch

    with patch.object(
        type(pane), "bridge", new_callable=PropertyMock, return_value=OfflineRuntimeBridge()
    ):
        text = await pane._slash_agents()
    assert "**Perfiles**" in text and "omnipotente" not in text


def test_reachable_qml_profile_surface_has_no_catalog_or_dead_global_controls():
    desktop = ROOT / "src/hermes/lumen/compositor/qml/desktop"
    profiles = (desktop / "AgentsApp.qml").read_text()
    dock = (desktop / "AppDock.qml").read_text()
    assert 'appId: "agents",     label: "Perfiles"' in dock
    assert 'text: "Perfiles"' in profiles
    assert '"list_agents"' in profiles and '"create_agent"' in profiles
    assert '"update_agent"' in profiles and '"delete_agent"' in profiles
    for obsolete in (
        "get_active_agent",
        "set_active_agent",
        "activeId",
        "omnipotente",
        'label: "Activar"',
    ):
        assert obsolete not in profiles
    for identity in RETIRED_FACTORY_IDS:
        assert identity not in profiles
    # Native dynamic delegation and observed task telemetry are not the retired
    # packaged catalog and must remain present.
    chat = (desktop / "ChatBar.qml").read_text()
    assert "sessions_fanout" in chat and "liveAgentRuns" in chat
    assert (desktop / "TasksApp.qml").exists()


def test_agent_app_awareness_routes_to_tasks_without_retired_catalog():
    prompt = NousReasoningEngine._chat_system_prompt(default_agent().to_persona())
    assert "Tareas (/tareas)" in prompt and "En vivo (/en-vivo)" in prompt
    assert "Agentes (/agentes)" not in prompt
    assert "pestaña Enseñar" not in prompt
