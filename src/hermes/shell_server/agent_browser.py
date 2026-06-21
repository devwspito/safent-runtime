"""AgentBrowser — gestor del Chromium del agente + endpoints REST.

Spawnea/mata el Chromium dedicado al agente (uid hermes-agent en producción,
o el mismo user en VM dev). Devuelve URL CDP para el frontend.

Spec 003 FR-035, FR-037 — Chromium en agents-os.slice con Restart=always.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes.security.browser_jail import _jail_enabled, push_egress_policy
from hermes.security.browser_launcher_client import (
    BrowserLauncherClient,
    BrowserLauncherUnavailable,
    BrowserLauncherError,
)

logger = logging.getLogger(__name__)

_DATA_DIR_DEFAULT = Path("/var/lib/hermes/chromium-agent-data")
# Teaching/mirror sessions are supervised by a human operator.
# They run in the same netns+jail but with open-logged egress policy
# (all navigation is audited, not filtered). See spec 009 §3.
_TEACHING_SESSION_NAME = "teaching-chromium"
_CHROMIUM_BIN_CANDIDATES = (
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/usr/lib64/chromium-browser/chromium-browser",
)
# Jailed browser: systemd (via hermes-browser-launcher) owns the Chromium process,
# so there is NO Popen handle in this process. The launcher relays the CDP onto the
# browser-netns veth IP at a fixed port (_CDP_BIND_ADDR:_CDP_PORT in the launcher).
# state() probes THIS live endpoint for the truth instead of a cached _proc handle.
_JAILED_CDP_HOST = "10.200.0.2"
_JAILED_CDP_PORT = 9333


def _jailed_cdp_alive() -> bool:
    """True if the jailed browser's CDP relay is accepting connections."""
    try:
        with socket.create_connection(
            (_JAILED_CDP_HOST, _JAILED_CDP_PORT), timeout=0.5
        ):
            return True
    except OSError:
        return False


class BrowserState(BaseModel):
    state: str  # 'stopped' | 'running' | 'starting'
    pid: int | None
    cdp_port: int | None
    cdp_url: str | None


class _BrowserController:
    """Singleton in-memory."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._cdp_port: int | None = None
        self._launcher_client = BrowserLauncherClient()

    def _find_binary(self) -> str | None:
        for path in _CHROMIUM_BIN_CANDIDATES:
            if os.path.exists(path):
                return path
        return shutil.which("chromium") or shutil.which("chromium-browser")

    def _free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def state(self) -> BrowserState:
        # Production (jail enabled): systemd owns the jailed Chromium — there is no
        # _proc handle, so the truth is the live CDP relay, not a cached handle.
        if _jail_enabled():
            if _jailed_cdp_alive():
                return BrowserState(
                    state="running",
                    pid=None,
                    cdp_port=_JAILED_CDP_PORT,
                    cdp_url=f"http://{_JAILED_CDP_HOST}:{_JAILED_CDP_PORT}",
                )
            return BrowserState(state="stopped", pid=None, cdp_port=None, cdp_url=None)
        # CI / dev (HERMES_BROWSER_JAIL=0): direct Popen handle.
        if self._proc is None:
            return BrowserState(
                state="stopped",
                pid=None,
                cdp_port=None,
                cdp_url=None,
            )
        rc = self._proc.poll()
        if rc is not None:
            self._proc = None
            self._cdp_port = None
            return BrowserState(state="stopped", pid=None, cdp_port=None, cdp_url=None)
        return BrowserState(
            state="running",
            pid=self._proc.pid,
            cdp_port=self._cdp_port,
            cdp_url=(
                f"http://127.0.0.1:{self._cdp_port}" if self._cdp_port else None
            ),
        )

    async def start(self) -> BrowserState:
        if self._proc is not None and self._proc.poll() is None:
            return self.state()
        binary = self._find_binary()
        if binary is None:
            raise RuntimeError("chromium binary no encontrado en PATH")
        data_dir = Path(
            os.environ.get(
                "HERMES_CHROMIUM_AGENT_DATA",
                str(_DATA_DIR_DEFAULT),
            )
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        port = self._free_port()
        argv = [
            binary,
            f"--user-data-dir={data_dir}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,SidePanel,Autofill,PasswordManager",
            # Sin llavero de GNOME: con autologin passwordless, Chromium pediría
            # la contraseña del login-keyring al arrancar (el "pair" que aparecía
            # al abrir el navegador en el teaching). store plano = cero prompt.
            "--password-store=basic",
            "--use-mock-keychain",
            "--start-maximized",
            # Finding D (DNS fix): resolve DNS via proxy (proxy-side DNS resolution).
            # The browser connects to sites via CONNECT, sending the hostname to the
            # proxy — no raw DNS queries escape the netns. The proxy resolves and audits.
            "--proxy-server=http://10.200.0.1:3128",
            "--proxy-bypass-list=<-loopback>",
            "about:blank",
        ]
        env = {
            **os.environ,
            "HOME": str(data_dir),
        }
        # Teaching/mirror mode: open-logged egress (human is watching).
        # Domains whitelist is empty → proxy allows all but audits every domain.
        push_egress_policy(
            session_name=_TEACHING_SESSION_NAME,
            domains_whitelist=(),
            teaching_mode=True,
        )

        # Wrap Chromium spawn with the root launcher (via AF_UNIX socket).
        # teaching_mode=True → open-logged policy (already pushed above).
        # The scope + netns + Landlock properties are HARDCODED in the launcher.
        # When HERMES_BROWSER_JAIL=0 (CI), fall back to direct Popen.
        if _jail_enabled():
            try:
                await self._launcher_client.launch(
                    session_name=_TEACHING_SESSION_NAME,
                    domains_whitelist=(),
                )
            except (BrowserLauncherUnavailable, BrowserLauncherError) as exc:
                # INVARIANT: no bare-argv fallback when jail is active.
                raise RuntimeError(
                    f"browser_jail active but launcher unavailable: {exc}"
                ) from exc
            # The launcher has already spawned the scope with the jail script.
            # We do not have a Popen handle here — systemd manages the process.
            logger.info(
                "chromium-agent jailed session=%s teaching_mode=True",
                _TEACHING_SESSION_NAME,
            )
            return self.state()

        # CI / dev path (HERMES_BROWSER_JAIL=0): direct Popen without confinement.
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
            "chromium-agent spawned pid=%s cdp=%s teaching_mode=True",
            self._proc.pid,
            port,
        )
        return self.state()

    def stop(self) -> BrowserState:
        if self._proc is None:
            return self.state()
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        self._proc = None
        self._cdp_port = None
        return self.state()


_browser_controller = _BrowserController()


def create_browser_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/browser", tags=["browser"])

    @router.get("", response_model=BrowserState)
    async def get_state() -> BrowserState:
        return _browser_controller.state()

    @router.post("/start", response_model=BrowserState)
    async def start_browser() -> BrowserState:
        try:
            return await _browser_controller.start()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))

    @router.post("/stop", response_model=BrowserState)
    async def stop_browser() -> BrowserState:
        return _browser_controller.stop()

    return router
