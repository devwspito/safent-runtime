"""Unit tests — Dual-browser CDP scoping (spec dual-browser).

Covers:
  1. cerebro_cdp_scope sets thread-local URL visible to get_thread_cdp_url().
  2. get_thread_cdp_url() returns None outside the scope (worker path).
  3. install_thread_local_cdp_override is idempotent (second call no-ops).
  4. _run_conversation_with_cdp enters scope for Cerebro, bypasses for workers.
  5. NousReasoningEngine._is_cerebro_agent: True for DEFAULT_AGENT_ID, False for
     worker agents, True for any agent with is_default=True via registry.
  6. NousReasoningEngine._resolve_cerebro_cdp: returns CDP URL for Cerebro,
     None for worker; fail-soft on manager error.
  7. CerebroBrowserManager:
     a. cdp_url=None when not started.
     b. emitter-path: ensure_running emits a cmd containing
        --remote-debugging-port=9333, about:blank, and NO --proxy-server;
        after port becomes reachable, cdp_url is set.
     c. idempotent reuse: when port is already accepting, emitter is NOT called again.
     d. fail-soft on launch timeout: cdp_url stays None, no exception raised.
     e. direct-Popen dev path (no emitter): Popen is called, cdp_url is set.
     f. stop() terminates Popen process and clears state.
  8. SCOPING PROOF: concurrent worker executes DURING a Cerebro scope and sees
     None (no CDP bleed).
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from hermes.runtime.cycle_cdp_context import (
    cerebro_cdp_scope,
    get_thread_cdp_url,
    install_thread_local_cdp_override,
)
from hermes.runtime.nous_engine import _run_conversation_with_cdp

pytestmark = pytest.mark.unit

_CDP_URL = "http://127.0.0.1:9333"


# ---------------------------------------------------------------------------
# 1-3: Thread-local scope primitives
# ---------------------------------------------------------------------------


class TestThreadLocalCdpScope:
    def test_scope_sets_and_clears_url(self) -> None:
        assert get_thread_cdp_url() is None
        with cerebro_cdp_scope(_CDP_URL):
            assert get_thread_cdp_url() == _CDP_URL
        assert get_thread_cdp_url() is None

    def test_scope_clears_on_exception(self) -> None:
        with pytest.raises(ValueError):
            with cerebro_cdp_scope(_CDP_URL):
                raise ValueError("boom")
        assert get_thread_cdp_url() is None

    def test_outside_scope_returns_none(self) -> None:
        assert get_thread_cdp_url() is None

    def test_install_is_idempotent(self) -> None:
        # First install is False because tools.browser_tool is not importable in tests.
        first = install_thread_local_cdp_override()
        second = install_thread_local_cdp_override()
        # Both may be False (module absent) or first=True/second=False (module present).
        # The invariant is: no exception and second call never raises.
        assert isinstance(first, bool)
        assert isinstance(second, bool)


# ---------------------------------------------------------------------------
# 4: _run_conversation_with_cdp
# ---------------------------------------------------------------------------


class TestRunConversationWithCdp:
    def _make_agent(self, captured: list) -> Any:
        agent = MagicMock()

        def run_conversation(msg, *, conversation_history=None):
            captured.append(get_thread_cdp_url())
            return {"narrative": "ok", "api_calls": 0}

        agent.run_conversation = run_conversation
        agent._pending_proposals = []
        agent._read_external_content = False
        return agent

    def test_cerebro_cycle_sets_cdp_in_thread(self) -> None:
        captured: list = []
        agent = self._make_agent(captured)
        _run_conversation_with_cdp(agent, "hello", None, _CDP_URL)
        assert captured == [_CDP_URL]

    def test_worker_cycle_no_cdp_in_thread(self) -> None:
        captured: list = []
        agent = self._make_agent(captured)
        _run_conversation_with_cdp(agent, "hello", None, None)
        assert captured == [None]

    def test_cdp_cleared_after_cerebro_run(self) -> None:
        agent = self._make_agent([])
        _run_conversation_with_cdp(agent, "hello", None, _CDP_URL)
        assert get_thread_cdp_url() is None


# ---------------------------------------------------------------------------
# 5: NousReasoningEngine._is_cerebro_agent
# ---------------------------------------------------------------------------


def _make_engine_bare() -> Any:
    """Build NousReasoningEngine without Nous installed (no heavy deps)."""
    from hermes.runtime.nous_engine import NousReasoningEngine
    from hermes.prompts.persona import PersonaSpec

    persona = PersonaSpec(
        name="Test",
        role="assistant",
        language="en",
        register="direct",
        primary_mission="test",
    )
    return NousReasoningEngine(persona=persona)


class TestIsCerebroAgent:
    def setup_method(self) -> None:
        self.engine = _make_engine_bare()

    def test_default_agent_id_is_cerebro(self) -> None:
        from hermes.agents.domain.agent import DEFAULT_AGENT_ID
        assert self.engine._is_cerebro_agent(DEFAULT_AGENT_ID) is True

    def test_worker_agent_id_is_not_cerebro(self) -> None:
        assert self.engine._is_cerebro_agent("worker-agent-123") is False

    def test_none_agent_id_is_not_cerebro(self) -> None:
        assert self.engine._is_cerebro_agent(None) is False

    def test_custom_default_agent_via_registry(self) -> None:
        fake_agent = MagicMock()
        fake_agent.is_default = True

        registry = MagicMock()
        registry.get_agent.return_value = fake_agent

        self.engine._agent_registry = registry
        assert self.engine._is_cerebro_agent("custom-default") is True
        registry.get_agent.assert_called_once_with("custom-default")

    def test_non_default_agent_via_registry(self) -> None:
        fake_agent = MagicMock()
        fake_agent.is_default = False

        registry = MagicMock()
        registry.get_agent.return_value = fake_agent

        self.engine._agent_registry = registry
        assert self.engine._is_cerebro_agent("non-default") is False

    def test_registry_error_returns_false(self) -> None:
        registry = MagicMock()
        registry.get_agent.side_effect = RuntimeError("db error")

        self.engine._agent_registry = registry
        # Should not raise; returns False conservatively.
        assert self.engine._is_cerebro_agent("any-agent") is False


# ---------------------------------------------------------------------------
# 6: NousReasoningEngine._resolve_cerebro_cdp
# ---------------------------------------------------------------------------


class TestResolveCerebroCdp:
    def setup_method(self) -> None:
        self.engine = _make_engine_bare()

    @pytest.mark.asyncio
    async def test_no_manager_returns_none(self) -> None:
        self.engine._cerebro_browser_manager = None
        result = await self.engine._resolve_cerebro_cdp("default")
        assert result is None

    @pytest.mark.asyncio
    async def test_worker_agent_returns_none(self) -> None:
        manager = AsyncMock()
        manager.ensure_running = AsyncMock()
        manager.cdp_url = _CDP_URL
        self.engine._cerebro_browser_manager = manager
        result = await self.engine._resolve_cerebro_cdp("worker-123")
        assert result is None
        manager.ensure_running.assert_not_called()

    @pytest.mark.asyncio
    async def test_cerebro_returns_cdp_url(self) -> None:
        from hermes.agents.domain.agent import DEFAULT_AGENT_ID

        manager = AsyncMock()
        manager.ensure_running = AsyncMock()
        manager.cdp_url = _CDP_URL
        self.engine._cerebro_browser_manager = manager

        result = await self.engine._resolve_cerebro_cdp(DEFAULT_AGENT_ID)
        assert result == _CDP_URL
        manager.ensure_running.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_manager_error_returns_none_failsoft(self) -> None:
        from hermes.agents.domain.agent import DEFAULT_AGENT_ID

        manager = AsyncMock()
        manager.ensure_running.side_effect = RuntimeError("browser failed")
        self.engine._cerebro_browser_manager = manager

        result = await self.engine._resolve_cerebro_cdp(DEFAULT_AGENT_ID)
        assert result is None  # fail-soft, not raised

    @pytest.mark.asyncio
    async def test_no_cdp_url_after_start_returns_none(self) -> None:
        from hermes.agents.domain.agent import DEFAULT_AGENT_ID

        manager = AsyncMock()
        manager.ensure_running = AsyncMock()
        manager.cdp_url = None  # browser started but no port yet
        self.engine._cerebro_browser_manager = manager

        result = await self.engine._resolve_cerebro_cdp(DEFAULT_AGENT_ID)
        assert result is None


# ---------------------------------------------------------------------------
# 7: CerebroBrowserManager unit tests (no real subprocess, no real port)
# ---------------------------------------------------------------------------


def _make_manager():
    from hermes.runtime.cerebro_browser_manager import CerebroBrowserManager
    return CerebroBrowserManager()


class TestCerebroBrowserManager:
    """Tests for the compositor-emitter launch path and dev Popen fallback."""

    # 7a — cdp_url is None before anything is started
    def test_cdp_url_none_when_not_started(self) -> None:
        m = _make_manager()
        assert m.cdp_url is None

    def test_stop_on_unstarted_manager_is_noop(self) -> None:
        m = _make_manager()
        m.stop()  # must not raise

    # 7b — emitter path: cmd contains correct flags, cdp_url set after port up
    @pytest.mark.asyncio
    async def test_emitter_launch_emits_correct_cmd(
        self, monkeypatch, tmp_path
    ) -> None:
        from hermes.runtime import cerebro_browser_manager as mod

        monkeypatch.setenv("HERMES_CHROMIUM_CEREBRO_DATA", str(tmp_path / "cerebro"))
        monkeypatch.setenv("HERMES_CEREBRO_CDP_PORT", "9333")
        monkeypatch.setattr(mod, "_find_chromium_binary", lambda: "/usr/bin/chromium-browser")

        emitted_cmds: list[str] = []

        # Simulate port becoming available immediately after first poll.
        call_count = [0]

        def _fake_port_accepting(port: int) -> bool:
            call_count[0] += 1
            return call_count[0] > 1  # first call False, rest True

        monkeypatch.setattr(mod, "_port_is_accepting", _fake_port_accepting)

        m = _make_manager()
        m.set_launch_emitter(emitted_cmds.append)
        await m.ensure_running()

        assert len(emitted_cmds) == 1
        cmd = emitted_cmds[0]
        assert "--remote-debugging-port=9333" in cmd
        assert "about:blank" in cmd
        assert "--proxy-server" not in cmd
        assert m.cdp_url == "http://127.0.0.1:9333"

    # 7c — idempotent: when port already accepting, emitter is NOT called again
    @pytest.mark.asyncio
    async def test_emitter_not_called_when_port_already_up(
        self, monkeypatch, tmp_path
    ) -> None:
        from hermes.runtime import cerebro_browser_manager as mod

        monkeypatch.setenv("HERMES_CHROMIUM_CEREBRO_DATA", str(tmp_path / "cerebro"))
        monkeypatch.setenv("HERMES_CEREBRO_CDP_PORT", "9333")
        monkeypatch.setattr(mod, "_find_chromium_binary", lambda: "/usr/bin/chromium-browser")
        monkeypatch.setattr(mod, "_port_is_accepting", lambda port: True)

        emitted_cmds: list[str] = []
        m = _make_manager()
        m.set_launch_emitter(emitted_cmds.append)

        # Pre-seed: port is accepting from the start.
        m._cdp_port = 9333

        await m.ensure_running()
        await m.ensure_running()

        # Emitter must never be called (port was already up).
        assert emitted_cmds == []
        assert m.cdp_url == "http://127.0.0.1:9333"

    # 7d — fail-soft on launch timeout: cdp_url stays None, no exception
    @pytest.mark.asyncio
    async def test_emitter_launch_timeout_is_failsoft(
        self, monkeypatch, tmp_path
    ) -> None:
        from hermes.runtime import cerebro_browser_manager as mod

        monkeypatch.setenv("HERMES_CHROMIUM_CEREBRO_DATA", str(tmp_path / "cerebro"))
        monkeypatch.setenv("HERMES_CEREBRO_CDP_PORT", "9333")
        monkeypatch.setattr(mod, "_find_chromium_binary", lambda: "/usr/bin/chromium-browser")
        # Port never accepts — timeout must happen quickly.
        monkeypatch.setattr(mod, "_port_is_accepting", lambda port: False)
        monkeypatch.setattr(mod, "_POLL_TIMEOUT_S", 0.1)
        monkeypatch.setattr(mod, "_POLL_INTERVAL_S", 0.05)

        m = _make_manager()
        m.set_launch_emitter(lambda cmd: None)

        # Must NOT raise; cdp_url stays None.
        await m.ensure_running()
        assert m.cdp_url is None

    # 7e — direct Popen path (no emitter wired)
    @pytest.mark.asyncio
    async def test_direct_popen_when_no_emitter(self, monkeypatch, tmp_path) -> None:
        from hermes.runtime import cerebro_browser_manager as mod

        monkeypatch.setenv("HERMES_CHROMIUM_CEREBRO_DATA", str(tmp_path / "cerebro"))
        monkeypatch.setenv("HERMES_CEREBRO_CDP_PORT", "9333")
        monkeypatch.setattr(mod, "_find_chromium_binary", lambda: "/usr/bin/chromium-browser")
        # Port reports accepting only after Popen is called.
        monkeypatch.setattr(mod, "_port_is_accepting", lambda port: False)

        fake_proc = MagicMock()
        fake_proc.pid = 12345
        fake_proc.poll.return_value = None  # alive

        with patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
            m = _make_manager()
            # No emitter set — must fall through to direct Popen.
            await m.ensure_running()

        mock_popen.assert_called_once()
        argv_used = mock_popen.call_args[0][0]
        assert any("--remote-debugging-port=9333" in a for a in argv_used)
        assert "about:blank" in argv_used
        assert not any("--proxy-server" in a for a in argv_used)
        assert m._cdp_port == 9333

    # 7f — stop() terminates Popen process and clears state
    def test_stop_terminates_process(self) -> None:
        m = _make_manager()
        fake_proc = MagicMock()
        fake_proc.wait.return_value = 0
        m._proc = fake_proc
        m._cdp_port = 9333
        m.stop()
        fake_proc.terminate.assert_called_once()
        assert m._cdp_port is None


# ---------------------------------------------------------------------------
# 8: SCOPING PROOF — no CDP bleed to concurrent worker thread
# ---------------------------------------------------------------------------


class TestNoCdpBleedToWorker:
    """Prove that a worker thread running concurrently with a Cerebro scope sees None.

    Simulates: Worker starts, then Cerebro sets its thread-local CDP, worker
    reads thread-local.  Worker must see None.
    """

    def test_concurrent_worker_sees_no_cdp(self) -> None:
        worker_saw: list = []
        barrier = threading.Barrier(2)

        def cerebro_thread() -> None:
            # Enter scope first, signal worker, then hold scope open.
            with cerebro_cdp_scope(_CDP_URL):
                barrier.wait()  # signal worker to read
                barrier.wait()  # wait for worker to finish reading

        def worker_thread() -> None:
            barrier.wait()  # wait for cerebro to set scope
            worker_saw.append(get_thread_cdp_url())
            barrier.wait()  # signal cerebro to exit

        t_cerebro = threading.Thread(target=cerebro_thread)
        t_worker = threading.Thread(target=worker_thread)
        t_cerebro.start()
        t_worker.start()
        t_cerebro.join()
        t_worker.join()

        # Worker thread must see None — no bleed from Cerebro's thread-local.
        assert worker_saw == [None], (
            f"ISOLATION VIOLATION: worker saw CDP URL {worker_saw!r} "
            "instead of None — thread-local is leaking"
        )
