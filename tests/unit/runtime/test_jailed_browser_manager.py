"""Unit tests — JailedBrowserManager.ensure_running() fast-path liveness check.

Bug: ensure_running()'s fast path required self._started (per-instance) to be
True before trusting the CDP port probe. Call sites such as
training_live._try_ensure_browser_running() and vnc_proxy's websocket handler
construct a FRESH JailedBrowserManager() on every call — self._started is
always False on a new instance even when the jailed browser is already alive
(e.g. launched by the eager boot-time start, or by a previous request). That
made every such call take the slow path and attempt another
BrowserLauncherClient.launch(), which fails outright when systemd-run finds the
transient unit name already active (spurious BrowserLauncherError /
JailedBrowserUnavailable against an otherwise-healthy browser).

Fix: the fast path now trusts _cdp_port_accepting(port) alone — a pure liveness
probe independent of which manager instance launched the browser.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hermes.runtime.jailed_browser_manager import JailedBrowserManager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_fresh_instance_skips_relaunch_when_port_already_accepting() -> None:
    """A brand-new manager (self._started=False) must NOT relaunch when the
    CDP port is already accepting — mirrors the real vnc_proxy/training_live
    call pattern of constructing JailedBrowserManager() per request."""
    mod = "hermes.runtime.jailed_browser_manager"
    with patch(f"{mod}._cdp_port_accepting", return_value=True):
        mgr = JailedBrowserManager()
        assert mgr._started is False  # fresh instance, never launched anything

        with patch.object(
            mgr, "_call_launcher", new_callable=AsyncMock
        ) as mock_launch:
            await mgr.ensure_running()
            mock_launch.assert_not_called()

        assert mgr._started is True  # liveness observed → bookkeeping updated
        assert mgr.cdp_url is not None


@pytest.mark.asyncio
async def test_ensure_running_still_launches_when_port_is_down() -> None:
    """Sanity check: the fix must not break the real launch path when the
    browser is genuinely absent."""
    mod = "hermes.runtime.jailed_browser_manager"
    with patch(f"{mod}._cdp_port_accepting", return_value=False):
        mgr = JailedBrowserManager()

        with patch.object(
            mgr, "_call_launcher", new_callable=AsyncMock
        ) as mock_launch, patch.object(
            mgr, "_poll_until_accepting", new_callable=AsyncMock
        ) as mock_poll:
            mock_poll.return_value = False
            from hermes.runtime.jailed_browser_manager import (
                JailedBrowserUnavailable,
            )

            with pytest.raises(JailedBrowserUnavailable):
                await mgr.ensure_running()
            mock_launch.assert_called_once()


# ---------------------------------------------------------------------------
# C1: per-session CDP port resolution (concurrent jailed-browser sessions)
# ---------------------------------------------------------------------------


class TestSessionNameParametrization:
    """JailedBrowserManager(session_name=...) resolves a DIFFERENT CDP port
    per session, mirroring hermes.security.browser_session_ports."""

    def test_default_constructor_uses_exec_browse(self) -> None:
        mgr = JailedBrowserManager()
        assert mgr._session_name == "exec-browse"

    def test_default_session_cdp_url_uses_legacy_port(self) -> None:
        mod = "hermes.runtime.jailed_browser_manager"
        with patch(f"{mod}._cdp_port_accepting", return_value=True):
            mgr = JailedBrowserManager()
            assert mgr.cdp_url == "http://10.200.0.2:9333"

    def test_other_session_cdp_url_uses_derived_port(self) -> None:
        mod = "hermes.runtime.jailed_browser_manager"
        with patch(f"{mod}._cdp_port_accepting", return_value=True):
            mgr = JailedBrowserManager(session_name="exec-abc123")
            from hermes.security.browser_session_ports import session_ports

            expected_port = session_ports("exec-abc123").cdp_port
            assert mgr.cdp_url == f"http://10.200.0.2:{expected_port}"
            assert expected_port != 9333

    @pytest.mark.asyncio
    async def test_launch_passes_session_name_to_launcher_client(self) -> None:
        mod = "hermes.runtime.jailed_browser_manager"
        captured: dict = {}

        async def _fake_launch(self, **kwargs):  # noqa: ANN001 — patches an unbound method
            captured.update(kwargs)

        from hermes.security.browser_launcher_client import BrowserLauncherClient

        with patch(f"{mod}._cdp_port_accepting", return_value=False), patch.object(
            BrowserLauncherClient, "launch", new=_fake_launch
        ):
            mgr = JailedBrowserManager(session_name="exec-xyz789")
            with patch.object(
                mgr, "_poll_until_accepting", new_callable=AsyncMock
            ) as mock_poll:
                mock_poll.return_value = True
                await mgr.ensure_running()

        assert captured.get("session_name") == "exec-xyz789"
