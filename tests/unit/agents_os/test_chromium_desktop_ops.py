"""Tests ChromiumDesktopOpsAdapter (FR-035, FR-037).

Note: ChromiumDesktopOpsAdapter.start() is now async (Finding B — launcher client).
All tests that call start() must be async or use asyncio.run().
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes.agents_os.infrastructure.chromium_desktop_ops import (
    ChromiumDesktopOpsAdapter,
    ChromiumState,
    FakeProcessRunner,
)
from hermes.security.browser_launcher_client import (
    BrowserLauncherClient,
    BrowserLauncherError,
    BrowserLauncherUnavailable,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def jail_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable browser jail in unit tests (no systemd-run in CI)."""
    monkeypatch.setenv("HERMES_BROWSER_JAIL", "0")


@pytest.fixture
def runner() -> FakeProcessRunner:
    return FakeProcessRunner()


@pytest.fixture
def adapter(runner: FakeProcessRunner) -> ChromiumDesktopOpsAdapter:
    return ChromiumDesktopOpsAdapter(runner=runner)


class TestStart:
    @pytest.mark.asyncio
    async def test_start_spawns_with_user_data_dir(
        self, adapter: ChromiumDesktopOpsAdapter, runner: FakeProcessRunner
    ) -> None:
        pid = await adapter.start()
        assert pid > 0
        assert adapter.state == ChromiumState.RUNNING
        argv, env = runner.spawns[0]
        assert any("--user-data-dir=" in a for a in argv)
        assert "Autofill" in " ".join(argv)
        assert env["HOME"]

    @pytest.mark.asyncio
    async def test_start_idempotent_when_running(
        self, adapter: ChromiumDesktopOpsAdapter
    ) -> None:
        pid1 = await adapter.start()
        pid2 = await adapter.start()
        assert pid1 == pid2

    @pytest.mark.asyncio
    async def test_proxy_server_flag_in_argv(
        self, adapter: ChromiumDesktopOpsAdapter, runner: FakeProcessRunner
    ) -> None:
        """Finding D: browser must use proxy for DNS resolution."""
        await adapter.start()
        argv, _ = runner.spawns[0]
        assert any("--proxy-server=http://10.200.0.1:3128" in a for a in argv)


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_terminates(
        self, adapter: ChromiumDesktopOpsAdapter, runner: FakeProcessRunner
    ) -> None:
        pid = await adapter.start()
        adapter.stop()
        assert adapter.state == ChromiumState.STOPPED
        assert pid in runner.terminations

    def test_stop_when_stopped_noop(
        self, adapter: ChromiumDesktopOpsAdapter
    ) -> None:
        adapter.stop()
        assert adapter.state == ChromiumState.STOPPED


class TestHealth:
    @pytest.mark.asyncio
    async def test_crash_detected(
        self, adapter: ChromiumDesktopOpsAdapter, runner: FakeProcessRunner
    ) -> None:
        pid = await adapter.start()
        runner.alive_pids.discard(pid)  # simula crash
        state = adapter.healthcheck()
        assert state == ChromiumState.CRASHED

    @pytest.mark.asyncio
    async def test_restart_if_crashed(
        self, adapter: ChromiumDesktopOpsAdapter, runner: FakeProcessRunner
    ) -> None:
        pid = await adapter.start()
        runner.alive_pids.discard(pid)
        restarted = await adapter.restart_if_crashed()
        assert restarted is True
        assert adapter.restart_count == 1
        assert adapter.state == ChromiumState.RUNNING

    @pytest.mark.asyncio
    async def test_restart_when_healthy_noop(
        self, adapter: ChromiumDesktopOpsAdapter
    ) -> None:
        await adapter.start()
        restarted = await adapter.restart_if_crashed()
        assert restarted is False


class TestEnv:
    @pytest.mark.asyncio
    async def test_env_overrides_applied(
        self, adapter: ChromiumDesktopOpsAdapter, runner: FakeProcessRunner
    ) -> None:
        await adapter.start(env_overrides={"WAYLAND_DISPLAY": "wayland-0"})
        _, env = runner.spawns[0]
        assert env["WAYLAND_DISPLAY"] == "wayland-0"

    @pytest.mark.asyncio
    async def test_extra_args_appended(
        self, adapter: ChromiumDesktopOpsAdapter, runner: FakeProcessRunner
    ) -> None:
        await adapter.start(extra_args=["--headless=new"])
        argv, _ = runner.spawns[0]
        assert "--headless=new" in argv


class TestJailIntegration:
    """Finding B: jail=1 routes through launcher client, NOT runner.spawn."""

    @pytest.mark.asyncio
    async def test_jail_on_calls_launcher_not_runner(
        self, runner: FakeProcessRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When jail=1, launcher client is called — not the FakeProcessRunner."""
        monkeypatch.setenv("HERMES_BROWSER_JAIL", "1")
        adapter = ChromiumDesktopOpsAdapter(runner=runner)
        ok_client = MagicMock()
        ok_client.launch = AsyncMock(return_value=None)
        adapter._launcher_client = ok_client

        with monkeypatch.context() as m:
            m.setenv("HERMES_BROWSER_JAIL", "1")
            await adapter.start()

        # Launcher was called.
        ok_client.launch.assert_called_once()
        # FakeProcessRunner was NOT used (scope is managed by systemd via launcher).
        assert len(runner.spawns) == 0

    @pytest.mark.asyncio
    async def test_jail_on_launcher_unavailable_raises(
        self, runner: FakeProcessRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finding B invariant: no bare-argv fallback when launcher fails."""
        monkeypatch.setenv("HERMES_BROWSER_JAIL", "1")
        adapter = ChromiumDesktopOpsAdapter(runner=runner)
        failing_client = MagicMock()
        failing_client.launch = AsyncMock(
            side_effect=BrowserLauncherUnavailable("socket missing")
        )
        adapter._launcher_client = failing_client

        with pytest.raises(RuntimeError, match="launcher unavailable"):
            await adapter.start()

        # FakeProcessRunner was NOT used as fallback.
        assert len(runner.spawns) == 0

    @pytest.mark.asyncio
    async def test_jail_on_launcher_error_raises(
        self, runner: FakeProcessRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HERMES_BROWSER_JAIL", "1")
        adapter = ChromiumDesktopOpsAdapter(runner=runner)
        failing_client = MagicMock()
        failing_client.launch = AsyncMock(
            side_effect=BrowserLauncherError("invalid session_name")
        )
        adapter._launcher_client = failing_client

        with pytest.raises(RuntimeError, match="launcher unavailable"):
            await adapter.start()
