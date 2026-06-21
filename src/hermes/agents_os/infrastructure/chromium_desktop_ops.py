"""ChromiumDesktopOpsAdapter — gestión del proceso Chromium del agente.

Spec 003 FR-035, FR-037 — Chromium dedicado al agente vive en la
slice `agents-os.slice` con Restart=always-with-backoff. Este adapter
encapsula su ciclo de vida (start/stop/restart/health) sin
acoplamiento al runtime.

Reglas:
  - El binario Chromium se ejecuta con `--user-data-dir` exclusivo
    para evitar contaminación con el navegador del humano (Epiphany
    o Firefox del usuario en personal-desktop).
  - El flag `--enable-features=Vulkan,UseSkiaRenderer` se evita por
    estabilidad en headless training.
  - El proceso corre como usuario `hermes-agent` (uid distinto al
    `hermes-user` humano) con Landlock heredado.

CI / tests:
  - subprocess no se ejecuta — todo dry_run o vía FakeProcessRunner.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from hermes.security.browser_jail import _jail_enabled, push_egress_policy
from hermes.security.browser_launcher_client import (
    BrowserLauncherClient,
    BrowserLauncherUnavailable,
    BrowserLauncherError,
)

logger = logging.getLogger(__name__)

_DEFAULT_CHROMIUM = "/usr/bin/chromium-browser"
_DEFAULT_DATA_DIR = Path("/var/lib/hermes/chromium-agent-data")


class ChromiumState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    CRASHED = "crashed"
    STOPPING = "stopping"


@runtime_checkable
class ProcessRunnerPort(Protocol):
    """Puerto contra subprocess para tests."""

    def spawn(self, argv: list[str], *, env: dict[str, str]) -> int: ...

    def is_alive(self, pid: int) -> bool: ...

    def terminate(self, pid: int, *, timeout_s: float = 5.0) -> None: ...


@dataclass(slots=True)
class FakeProcessRunner:
    next_pid: int = 1000
    alive_pids: set[int] = field(default_factory=set)
    spawns: list[tuple[list[str], dict[str, str]]] = field(default_factory=list)
    terminations: list[int] = field(default_factory=list)

    def spawn(self, argv: list[str], *, env: dict[str, str]) -> int:
        pid = self.next_pid
        self.next_pid += 1
        self.alive_pids.add(pid)
        self.spawns.append((argv, dict(env)))
        return pid

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive_pids

    def terminate(self, pid: int, *, timeout_s: float = 5.0) -> None:
        self.terminations.append(pid)
        self.alive_pids.discard(pid)


_DESKTOP_SESSION_NAME = "desktop-chromium"


@dataclass(slots=True)
class ChromiumDesktopOpsAdapter:
    """Gestor del Chromium del agente.

    Teaching / mirror mode: el humano supervisa la sesión en tiempo real.
    El jail se aplica igual (mismo netns + Landlock + systemd scope) pero
    la política de egress es open-logged — toda navegación auditada, no
    filtrada. Ver spec 009 §3 (asimetría teaching/autónomo).
    """

    runner: ProcessRunnerPort
    chromium_path: str = _DEFAULT_CHROMIUM
    user_data_dir: Path = _DEFAULT_DATA_DIR
    state: ChromiumState = ChromiumState.STOPPED
    current_pid: int | None = None
    last_started_at: datetime | None = None
    restart_count: int = 0
    # Teaching mode: open-logged egress policy (human is watching).
    # Set to True from TeachingSessionOrchestrator; False = autonomous.
    teaching_mode: bool = True
    _launcher_client: BrowserLauncherClient = field(
        default_factory=BrowserLauncherClient, compare=False
    )

    async def start(
        self,
        *,
        extra_args: list[str] | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> int:
        if self.state == ChromiumState.RUNNING:
            return self.current_pid  # type: ignore[return-value]
        self.state = ChromiumState.STARTING
        argv = [
            self.chromium_path,
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,SidePanel",
            # FR-035: el agente nunca abre el wallet ni autofill.
            "--disable-features=Autofill,PasswordManager",
            # Sin llavero GNOME (autologin passwordless → prompt de contraseña del
            # login-keyring al abrir). store plano = cero prompt.
            "--password-store=basic",
            "--use-mock-keychain",
            # Finding D (DNS fix): resolve DNS via proxy (proxy-side DNS resolution).
            # The browser connects to sites via CONNECT, sending the hostname to the
            # proxy — no raw DNS queries escape the netns. The proxy resolves and audits.
            "--proxy-server=http://10.200.0.1:3128",
            "--proxy-bypass-list=<-loopback>",
            # Headless es opcional — en personal-desktop necesitamos
            # ventanas visibles para training; en server siempre headless.
            *(extra_args or []),
        ]
        env = {
            "HOME": str(self.user_data_dir),
            "XDG_RUNTIME_DIR": "/run/user/1001",
            **(env_overrides or {}),
        }
        # Teaching/desktop sessions are always open-logged (human operator supervising).
        # See spec 009 §3: asimetría teaching/autónomo.
        push_egress_policy(
            session_name=_DESKTOP_SESSION_NAME,
            domains_whitelist=(),
            teaching_mode=self.teaching_mode,
        )

        # Route through the root launcher when jail is active (Finding B).
        # The launcher holds the hardcoded systemd-run property template;
        # this process (User=hermes) cannot call systemd-run --scope directly.
        # When jail is disabled (CI), use the FakeProcessRunner path.
        if _jail_enabled():
            try:
                await self._launcher_client.launch(
                    session_name=_DESKTOP_SESSION_NAME,
                    domains_whitelist=(),
                )
            except (BrowserLauncherUnavailable, BrowserLauncherError) as exc:
                # INVARIANT: no bare-argv fallback when jail is active.
                raise RuntimeError(
                    f"chromium_desktop_ops: jail active but launcher unavailable: {exc}"
                ) from exc
            # systemd manages the scope PID — no Popen handle. Return a sentinel.
            self.current_pid = -1
            self.last_started_at = datetime.now(tz=UTC)
            self.state = ChromiumState.RUNNING
            logger.info(
                "chromium_desktop jailed session=%s teaching_mode=%s",
                _DESKTOP_SESSION_NAME,
                self.teaching_mode,
            )
            return -1

        pid = self.runner.spawn(argv, env=env)
        self.current_pid = pid
        self.last_started_at = datetime.now(tz=UTC)
        self.state = ChromiumState.RUNNING
        return pid

    def stop(self) -> None:
        if self.state == ChromiumState.STOPPED:
            return
        self.state = ChromiumState.STOPPING
        if self.current_pid is not None:
            self.runner.terminate(self.current_pid)
        self.current_pid = None
        self.state = ChromiumState.STOPPED

    def healthcheck(self) -> ChromiumState:
        if self.current_pid is None:
            if self.state != ChromiumState.STOPPED:
                self.state = ChromiumState.STOPPED
            return self.state
        alive = self.runner.is_alive(self.current_pid)
        if not alive and self.state == ChromiumState.RUNNING:
            self.state = ChromiumState.CRASHED
        return self.state

    async def restart_if_crashed(self) -> bool:
        state = self.healthcheck()
        if state != ChromiumState.CRASHED:
            return False
        self.current_pid = None
        self.state = ChromiumState.STOPPED
        self.restart_count += 1
        await self.start()
        return True


class HostSubprocessRunner:  # pragma: no cover — requires real host
    """Runner real con subprocess. Solo se importa en nodo."""

    def spawn(self, argv: list[str], *, env: dict[str, str]) -> int:
        proc = subprocess.Popen(  # noqa: S603
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info(
            "chromium-agent spawned pid=%s argv=%s", proc.pid, shlex.join(argv)
        )
        return proc.pid

    def is_alive(self, pid: int) -> bool:
        try:
            import os

            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def terminate(self, pid: int, *, timeout_s: float = 5.0) -> None:
        import os
        import signal
        import time

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.is_alive(pid):
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
