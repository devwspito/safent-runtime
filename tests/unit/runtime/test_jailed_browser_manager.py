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
