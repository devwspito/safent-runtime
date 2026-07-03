"""JailedBrowserManager — headless Chromium confined inside hermes-browser netns.

Launches a headless Chromium INSIDE the hermes-browser network namespace via
BrowserLauncherClient, binding its CDP listener on the netns-side veth IP
(10.200.0.2). The daemon (host netns) reaches it over the veth peer at
10.200.0.2:9333.

Confinement model:
  - ALL browser egress is forced through the Squid proxy at 10.200.0.1:3128
    (host side of the veth); nftables inside the netns drops direct WAN.
  - CDP control plane is accessible to the daemon via 10.200.0.2:9333;
    the browser-ns.nft input chain must have an explicit accept rule for
    10.200.0.1→10.200.0.2:9333 (daemon's veth gateway).
  - No fallback to a host-netns Chromium: if the jailed browser is
    unavailable the caller receives JailedBrowserUnavailable and the seatbelt
    in cycle_cdp_context prevents a silent unconfined spawn.

Capa: infrastructure (adapts BrowserLauncherClient to a domain-usable interface).
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("hermes.jailed_browser")

# ── Constants (all literals — no caller/LLM input) ────────────────────────────
# NOTE: the actual Chromium argv (headless, CDP addr/port, proxy, user-data-dir)
# is built SERVER-SIDE by hermes-browser-launcher (the privilege boundary), NOT
# here — this manager only requests a launch by session_name and then polls the
# CDP port. _CDP_BIND_HOST/_CDP_PORT_DEFAULT MUST match the launcher constants
# (_CDP_BIND_ADDR / _CDP_PORT in hermes-browser-launcher).

_CDP_BIND_HOST = "10.200.0.2"

_CDP_PORT_DEFAULT = 9333

_SESSION_NAME = "exec-browse"

_POLL_INTERVAL_S = 0.4
_POLL_TIMEOUT_S = 25.0
_CDP_CHECK_TIMEOUT_S = 2.0


def _resolve_cdp_port() -> int:
    raw = os.environ.get("HERMES_JAILED_CDP_PORT", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning(
                "hermes.jailed_browser.invalid_cdp_port HERMES_JAILED_CDP_PORT=%r "
                "— falling back to default %d",
                raw,
                _CDP_PORT_DEFAULT,
            )
    return _CDP_PORT_DEFAULT


def _cdp_port_accepting(port: int) -> bool:
    """True if Chromium's CDP is actually SERVING at 10.200.0.2:<port>.

    A bare TCP connect is not enough: a socat relay (the jail exposes Chromium's
    loopback CDP on the veth IP) accepts connections immediately, before Chromium
    is up. So we GET /json/version and require a valid CDP response — confirming
    end-to-end reachability (daemon → veth → socat → Chromium) before we declare
    the browser ready and publish BROWSER_CDP_URL.
    """
    import json as _json  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    url = f"http://{_CDP_BIND_HOST}:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=_CDP_CHECK_TIMEOUT_S) as resp:  # noqa: S310
            if resp.status != 200:
                return False
            data = _json.loads(resp.read(8192).decode("utf-8", "replace"))
            return "webSocketDebuggerUrl" in data or "Browser" in data
    except Exception:  # noqa: BLE001 — not ready yet / unreachable
        return False


# ── Exception ─────────────────────────────────────────────────────────────────


class JailedBrowserUnavailable(RuntimeError):
    """The jailed headless Chromium could not be started or is unreachable.

    Raised by JailedBrowserManager.ensure_running() when the browser fails to
    bind its CDP port within the poll timeout. Callers must NOT fall back to a
    host-netns Chromium — the seatbelt in cycle_cdp_context enforces this.
    """


# ── Manager ───────────────────────────────────────────────────────────────────


class JailedBrowserManager:
    """Manages a headless Chromium inside the hermes-browser network namespace.

    Lifecycle:
      1. ensure_running() — idempotent; launches if not yet started or if the
         CDP port stopped accepting. Raises JailedBrowserUnavailable on failure.
      2. cdp_url property — returns the CDP endpoint URL or None (fail-soft).

    Thread safety: a single asyncio.Lock serialises concurrent ensure_running()
    calls. cdp_url is safe to call from any thread (pure TCP probe, no state
    mutation).
    """

    def __init__(self) -> None:
        self._started: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()

    async def ensure_running(self) -> None:
        """Ensure the jailed headless Chromium is alive; launch if needed.

        Fast path: if the CDP port accepts, a healthy browser is already running
        — return immediately WITHOUT relaunching. This check does NOT depend on
        self._started: callers (e.g. the vnc_proxy/training_live websocket
        handlers) construct a FRESH JailedBrowserManager() per request, so
        self._started is always False on that instance even though the eager
        boot-time singleton (or another instance) already launched the browser.
        Gating the fast path on self._started made ensure_running() relaunch
        (via a redundant systemd-run) on every single call from a fresh
        instance, even against an already-healthy browser — wasted work at
        best, and a spurious BrowserLauncherError/JailedBrowserUnavailable at
        worst (systemd-run fails outright when the transient unit name is
        already active).
        Slow path (under lock): delegates launch to BrowserLauncherClient, then
        polls the CDP port for up to _POLL_TIMEOUT_S seconds.

        Raises:
            JailedBrowserUnavailable: if the launcher fails or the port does
                not accept within the timeout. Fail-closed — NO host fallback.
        """
        port = _resolve_cdp_port()

        # Fast path: the port is live — a browser is already running, launched
        # by this instance, another instance, or the boot-time eager start.
        if _cdp_port_accepting(port):
            self._started = True
            return

        async with self._lock:
            # Re-check under lock (another coroutine may have just launched).
            if _cdp_port_accepting(port):
                self._started = True
                return

            self._started = False
            await self._launch(port)

    @property
    def cdp_url(self) -> str | None:
        """Return http://10.200.0.2:<port> if the CDP port accepts, else None.

        Fail-soft: returns None on any socket error. Does not raise.
        """
        port = _resolve_cdp_port()
        try:
            if _cdp_port_accepting(port):
                return f"http://{_CDP_BIND_HOST}:{port}"
        except Exception:  # noqa: BLE001 — fail-soft getter
            logger.debug(
                "hermes.jailed_browser.cdp_url_check_failed", exc_info=True
            )
        return None

    # ── Private ───────────────────────────────────────────────────────────────

    async def _launch(self, port: int) -> None:
        logger.info(
            "hermes.jailed_browser.launching session=%s port=%d",
            _SESSION_NAME,
            port,
        )

        await self._call_launcher()

        success = await self._poll_until_accepting(port)
        if not success:
            raise JailedBrowserUnavailable(
                f"hermes.jailed_browser.launch_timeout: CDP port "
                f"{_CDP_BIND_HOST}:{port} not accepting after "
                f"{_POLL_TIMEOUT_S}s — jailed browser unavailable"
            )

        self._started = True
        logger.info(
            "hermes.jailed_browser.ready cdp_port=%d session=%s",
            port,
            _SESSION_NAME,
        )

    async def _call_launcher(self) -> None:
        from hermes.security.browser_launcher_client import (  # noqa: PLC0415
            BrowserLauncherClient,
            BrowserLauncherError,
            BrowserLauncherUnavailable,
        )

        client = BrowserLauncherClient()
        try:
            # The launcher builds the full Chromium argv server-side from
            # session_name (security HIGH-1); we pass no argv. domains empty →
            # the egress proxy runs open-logged (autonomous discovery posture).
            await client.launch(
                session_name=_SESSION_NAME,
                domains_whitelist=(),
            )
        except BrowserLauncherUnavailable as exc:
            raise JailedBrowserUnavailable(
                f"hermes.jailed_browser.launcher_unavailable: {exc}"
            ) from exc
        except BrowserLauncherError as exc:
            raise JailedBrowserUnavailable(
                f"hermes.jailed_browser.launcher_error: {exc}"
            ) from exc

    async def _poll_until_accepting(self, port: int) -> bool:
        elapsed = 0.0
        while elapsed < _POLL_TIMEOUT_S:
            if _cdp_port_accepting(port):
                return True
            await asyncio.sleep(_POLL_INTERVAL_S)
            elapsed += _POLL_INTERVAL_S
        return False
