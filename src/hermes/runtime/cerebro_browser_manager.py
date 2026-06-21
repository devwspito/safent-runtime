"""CerebroBrowserManager — singleton headed Chromium for the Cerebro (default agent).

The Cerebro's browser must be VISIBLE on the desktop (wayland-0). When the
Cerebro calls browser_navigate/browser_click, the underlying browser_tool reads
BROWSER_CDP_URL (or browser.cdp_url in config.yaml) and connects via CDP to an
EXISTING headed browser rather than launching a local headless one.

Launch path (production):
  The daemon (User=hermes) cannot open a Wayland display directly. Instead it
  delegates launch to the compositor (User=hermes-user) via the AppLaunchRequested
  D-Bus signal. DbusRuntimeAdapter.start() injects a launch_emitter callable into
  this manager (same pattern as AppLaunchSurfaceAdapter). The compositor's
  sys_manager.launchNativeApp(cmd) does shlex.split(cmd) and runs chromium with
  WAYLAND_DISPLAY=wayland-0, guaranteeing the window is visible.

  The browser opens at a FIXED CDP port (HERMES_CEREBRO_CDP_PORT, default 9333)
  that is known before launch, so the daemon can poll 127.0.0.1:<port> after
  emitting the launch signal (both share the host netns — daemon has no
  PrivateNetwork).

  After emitting the signal, ensure_running() polls TCP port 9333 up to ~20 s.
  On success, _cdp_port is set and cdp_url returns the URL. On timeout the
  manager logs an error and leaves cdp_url=None (fail-soft: the engine cycle
  must not crash on a browser launch failure).

Launch path (CI / dev — no emitter wired):
  Direct subprocess.Popen with WAYLAND_DISPLAY=wayland-0. Used when
  HERMES_CEREBRO_BROWSER=0 env disables the manager, or when no emitter is
  injected (e.g. unit tests with a patched Popen). No proxy-server flag.

  NOTE: if an emitter IS set, it is ALWAYS preferred over direct Popen,
  regardless of HERMES_BROWSER_JAIL or any other flag.

Visible-window note:
  The about:blank window produced by the compositor-emitter path is always
  visible on the desktop. This is expected: the Cerebro's browser is an
  owner-watched surface; egress is open-logged (same posture as teaching mode).
  Hiding the window is a UX concern for a future iteration, not a security one.

CDP port:
  Fixed at HERMES_CEREBRO_CDP_PORT (default 9333). Dynamic port discovery
  (_free_port) is removed: the compositor launch is fire-and-signal (no PID
  returned), so the daemon must know the port in advance.

Liveness:
  - Emitter path: liveness is determined by TCP connectivity to 127.0.0.1:<port>.
    There is no process handle. Crash recovery: on the next ensure_running() call
    that finds the port closed, the emitter is fired again (the compositor will
    spawn a new chromium process).
  - Direct Popen path (dev): liveness via subprocess.Popen.poll().

Capa: infrastructure (adapts OS subprocess / D-Bus emitter to a domain concept).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR_DEFAULT = Path("/var/lib/hermes/chromium-cerebro-data")

_CDP_PORT_DEFAULT = 9333
_POLL_INTERVAL_S = 0.25
_POLL_TIMEOUT_S = 20.0

_CHROMIUM_BIN_CANDIDATES: tuple[str, ...] = (
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/usr/lib64/chromium-browser/chromium-browser",
)

# Injected explicitly so a direct-Popen dev fallback still lands on-screen.
_WAYLAND_DISPLAY = "wayland-0"


def _find_chromium_binary() -> str | None:
    for path in _CHROMIUM_BIN_CANDIDATES:
        if os.path.exists(path):
            return path
    return shutil.which("chromium") or shutil.which("chromium-browser")


def _resolve_cdp_port() -> int:
    raw = os.environ.get("HERMES_CEREBRO_CDP_PORT", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning(
                "hermes.cerebro_browser.invalid_cdp_port HERMES_CEREBRO_CDP_PORT=%r "
                "— falling back to default %d",
                raw,
                _CDP_PORT_DEFAULT,
            )
    return _CDP_PORT_DEFAULT


def _port_is_accepting(port: int) -> bool:
    """Return True if 127.0.0.1:<port> is accepting TCP connections."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _build_chromium_argv(binary: str, data_dir: Path, port: int) -> list[str]:
    """Build the hardcoded Chromium argv for the Cerebro's headed browser.

    Security: every flag is a literal string constant — no LLM/caller input is
    interpolated.  No --proxy-server: the compositor launches in the host netns,
    which has no access to the jail's 10.200.0.1:3128 proxy.  Egress is
    open-logged (owner-watched posture, same as teaching mode).
    """
    return [
        binary,
        f"--user-data-dir={data_dir}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,SidePanel,Autofill,PasswordManager",
        "--password-store=basic",
        "--use-mock-keychain",
        "--start-maximized",
        # about:blank: ventana inicial mínima (CDP en 9333 liga bien). Se probó
        # --no-startup-window para evitar la ventana idle, pero impedía que el CDP
        # ligara -> browser_navigate caía a headless invisible. El lazy-launch ya
        # evita abrir Chromium salvo al navegar; unificar a 1 ventana = overhaul
        # de gestión de ventanas (motion spec: open/close/focus/maximize).
        "about:blank",
    ]


class CerebroBrowserManager:
    """Singleton-per-owner headed Chromium for the Cerebro agent.

    Lifecycle:
      1. set_launch_emitter(emitter) — called by DbusRuntimeAdapter.start()
         after the bus is connected. Until then the manager uses direct Popen
         (dev/CI path).
      2. ensure_running() — lazy launch; idempotent if port is already alive.
      3. cdp_url property — returns "http://127.0.0.1:<port>" or None.
      4. stop() — terminate direct-Popen process (daemon shutdown / test teardown).
         Emitter-launched processes are owned by the compositor; stop() only
         clears internal state so the next ensure_running() re-checks liveness.
    """

    def __init__(self) -> None:
        self._cdp_port: int | None = None
        self._launch_emitter: Callable[[str], None] | None = None
        # Direct-Popen handle; None when launched via emitter.
        self._proc: subprocess.Popen | None = None

    # ------------------------------------------------------------------
    # Emitter injection (called by DbusRuntimeAdapter.start())
    # ------------------------------------------------------------------

    def set_launch_emitter(self, emitter: Callable[[str], None]) -> None:
        """Inject the compositor launch emitter after the D-Bus bus starts."""
        self._launch_emitter = emitter
        logger.info("hermes.cerebro_browser.emitter_injected")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ensure_running(self) -> None:
        """Ensure the headed Chromium is alive. Launches lazily; restarts on crash.

        Safe to call multiple times per cycle (fast path: TCP connectivity check
        on a known fixed port).
        """
        port = _resolve_cdp_port()

        if self._cdp_port is not None and _port_is_accepting(port):
            return

        # Reset stale state (e.g. after a crash or first call).
        self._cdp_port = None

        await self._launch(port)

    @property
    def cdp_url(self) -> str | None:
        """CDP endpoint URL or None if the browser is not reachable."""
        if self._cdp_port is None:
            return None
        if not _port_is_accepting(self._cdp_port):
            return None
        return f"http://127.0.0.1:{self._cdp_port}"

    def stop(self) -> None:
        """Terminate a direct-Popen browser (dev path). Clears state for emitter path."""
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            self._proc = None
        # Always clear port: next ensure_running() will confirm liveness.
        self._cdp_port = None

    # ------------------------------------------------------------------
    # Private: liveness and launch
    # ------------------------------------------------------------------

    def _is_alive(self) -> bool:
        """Check liveness via TCP when emitter-launched, via poll() otherwise."""
        port = _resolve_cdp_port()
        if self._proc is not None:
            rc = self._proc.poll()
            if rc is not None:
                logger.warning(
                    "hermes.cerebro_browser.crashed rc=%s — will restart on next ensure",
                    rc,
                )
                self._proc = None
                self._cdp_port = None
                return False
            return True
        # Emitter-launched or not yet started: TCP is the ground truth.
        return self._cdp_port is not None and _port_is_accepting(port)

    async def _launch(self, port: int) -> None:
        """Launch the headed Chromium via compositor emitter or direct Popen."""
        binary = _find_chromium_binary()
        if binary is None:
            logger.error(
                "hermes.cerebro_browser.binary_not_found — "
                "install chromium-browser to enable headed browser"
            )
            return

        data_dir = Path(
            os.environ.get("HERMES_CHROMIUM_CEREBRO_DATA", str(_DATA_DIR_DEFAULT))
        )
        data_dir.mkdir(parents=True, exist_ok=True)

        argv = _build_chromium_argv(binary, data_dir, port)

        if self._launch_emitter is not None:
            await self._launch_via_emitter(argv, port)
        else:
            self._launch_direct(argv, port)

    async def _launch_via_emitter(self, argv: list[str], port: int) -> None:
        """Launch via the compositor's AppLaunchRequested signal, then poll port."""
        cmd = " ".join(argv)
        logger.info(
            "hermes.cerebro_browser.emitter_launch port=%d cmd=%r",
            port,
            cmd,
        )
        self._launch_emitter(cmd)  # type: ignore[misc]  # guarded above

        success = await self._poll_until_accepting(port)
        if success:
            self._cdp_port = port
            logger.info(
                "hermes.cerebro_browser.emitter_launch_ready cdp_port=%d",
                port,
            )
        else:
            logger.error(
                "hermes.cerebro_browser.emitter_launch_timeout "
                "cdp_port=%d timeout_s=%s — browser not reachable; "
                "Cerebro will use headless fallback this cycle",
                port,
                _POLL_TIMEOUT_S,
            )

    async def _poll_until_accepting(self, port: int) -> bool:
        """Poll 127.0.0.1:<port> until it accepts or timeout expires."""
        elapsed = 0.0
        while elapsed < _POLL_TIMEOUT_S:
            if _port_is_accepting(port):
                return True
            await asyncio.sleep(_POLL_INTERVAL_S)
            elapsed += _POLL_INTERVAL_S
        return False

    def _launch_direct(self, argv: list[str], port: int) -> None:
        """Direct Popen — dev/CI path when no emitter is wired."""
        env = {
            **os.environ,
            "HOME": argv[1].split("=", 1)[1],  # --user-data-dir=<path>
            "WAYLAND_DISPLAY": _WAYLAND_DISPLAY,
            "QT_QPA_PLATFORM": "wayland",
            "GDK_BACKEND": "wayland",
            "XDG_SESSION_TYPE": "wayland",
        }
        self._proc = subprocess.Popen(  # noqa: S603
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._cdp_port = port
        logger.info(
            "hermes.cerebro_browser.direct_launched pid=%s cdp_port=%s wayland=%s",
            self._proc.pid,
            port,
            _WAYLAND_DISPLAY,
        )
