"""Lumen Terminal (hermes.tui) — unit + Pilot smoke tests.

Pure/offline: drives the app with OfflineRuntimeBridge via Textual's headless
Pilot. Skipped automatically if textual isn't installed (it's a [tui] extra).
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("textual")

from hermes.tui.app import LumenTerminal  # noqa: E402
from hermes.tui.bridge import OfflineRuntimeBridge, new_conversation_id  # noqa: E402

pytestmark = pytest.mark.unit

PANES = (
    "chat", "tasks", "agents", "skills", "integrations",
    "security", "scheduler", "memory", "providers", "packages",
)


async def test_offline_bridge_typed_helpers() -> None:
    b = OfflineRuntimeBridge()
    await b.connect()
    agents = await b.list_agents()
    assert isinstance(agents, list)
    assert any(a.get("is_default") for a in agents), "Cerebro (default) debe existir"
    qs = await b.get_queue_status()
    assert "state" in qs
    saved = await b.add_provider(
        {"kind": "openai", "alias": "x", "default_model": "m", "set_active": True}
    )
    assert saved.get("provider_id"), "add_provider debe devolver un provider_id"
    assert await b.delete_provider("any") is True


def test_new_conversation_id_is_unique() -> None:
    assert new_conversation_id() != new_conversation_id()


async def test_app_boots_and_navigates_all_panes() -> None:
    app = LumenTerminal(bridge=OfflineRuntimeBridge())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        from textual.widgets import ContentSwitcher

        cs = app.query_one("#content", ContentSwitcher)
        for pane in PANES:
            app.go_to(pane)
            await pilot.pause()
            assert cs.current == f"pane-{pane}", f"{pane}: switcher={cs.current}"


async def test_chat_streams_offline() -> None:
    from hermes.tui.screens.chat import ChatMessage, ChatPane

    app = LumenTerminal(bridge=OfflineRuntimeBridge())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        await chat._send("hola")
        for _ in range(60):
            await pilot.pause()
            if not chat._streaming:
                break
        assert not chat._streaming, "el stream offline debe terminar"
        msgs = list(app.query(ChatMessage))
        assert len(msgs) >= 3, "welcome + user + agent"
        assert msgs[-1].text.strip(), "el agente debe haber pintado texto"


async def test_provider_add_form_opens() -> None:
    from hermes.tui.modals.common import FormModal

    app = LumenTerminal(bridge=OfflineRuntimeBridge())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.go_to("providers")
        await pilot.pause()
        app.query_one("#prov-table").focus()
        await pilot.press("n")
        await pilot.pause()
        assert any(isinstance(s, FormModal) for s in app.screen_stack)


async def test_approval_modal_opens_on_signal() -> None:
    from hermes.tui import messages as M
    from hermes.tui.modals.approval import ApprovalModal

    app = LumenTerminal(bridge=OfflineRuntimeBridge())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.post_message(
            M.ApprovalRequested(
                json.dumps({"tool": "delete_file", "risk": "high", "proposal_id": "p1"})
            )
        )
        await pilot.pause()
        await pilot.pause()
        assert any(isinstance(s, ApprovalModal) for s in app.screen_stack)


async def test_kill_switch_toggles_pause() -> None:
    app = LumenTerminal(bridge=OfflineRuntimeBridge())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        assert app._paused is False
        await app._toggle_pause()
        assert app._paused is True
        await app._toggle_pause()
        assert app._paused is False


async def test_slash_commands_render_listings() -> None:
    from hermes.tui.screens.chat import ChatMessage, ChatPane

    app = LumenTerminal(bridge=OfflineRuntimeBridge())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        chat = app.query_one(ChatPane)
        for cmd in ("/help", "/mcp", "/skills", "/integrations", "/providers", "/security"):
            await chat._handle_slash(cmd)
            await pilot.pause()
        texts = " ".join(m.text for m in app.query(ChatMessage))
        assert "Comandos" in texts  # /help rendered
        assert "Centro de seguridad" in texts  # /security rendered


async def test_security_review_modal_on_signal() -> None:
    from hermes.tui import messages as M
    from hermes.tui.modals.security_review import SecurityReviewModal

    app = LumenTerminal(bridge=OfflineRuntimeBridge())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.post_message(
            M.InstallReviewRequested(
                "scan1",
                json.dumps({"kind": "skill", "identifier": "x", "score": 40,
                            "verdict": "review", "risks": [{"title": "telemetry"}]}),
            )
        )
        await pilot.pause()
        await pilot.pause()
        assert any(isinstance(s, SecurityReviewModal) for s in app.screen_stack)


async def test_skills_hub_modal_opens() -> None:
    from hermes.tui.modals.search_install import SearchInstallModal

    app = LumenTerminal(bridge=OfflineRuntimeBridge())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.go_to("skills")
        await pilot.pause()
        app.query_one("#skills-table").focus()
        await pilot.press("h")
        await pilot.pause()
        assert any(isinstance(s, SearchInstallModal) for s in app.screen_stack)


async def test_packages_search_modal_opens() -> None:
    from hermes.tui.modals.search_install import SearchInstallModal

    app = LumenTerminal(bridge=OfflineRuntimeBridge())
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app.go_to("packages")
        await pilot.pause()
        app.query_one("#pkg-table").focus()
        await pilot.press("s")
        await pilot.pause()
        assert any(isinstance(s, SearchInstallModal) for s in app.screen_stack)
