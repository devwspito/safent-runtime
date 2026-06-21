"""AgentBrowserCli: adaptador real del binario agent-browser (vercel-labs).

Lazy: el binario se verifica una sola vez en start() via shutil.which.
Si no esta instalado levanta AgentBrowserNotInstalledError — nunca en import.

Modelo de sesion (del README de agent-browser):
  - Un daemon persistente se lanza automaticamente en el primer comando y
    permanece vivo manteniendo la conexion browser/CDP. Cada invocacion de
    `agent-browser <cmd>` reutiliza el MISMO browser — el estado (cookies,
    tabs) vive en el daemon, NO en el proceso CLI.
  - Aislamiento: `--session <name>` da un browser aislado por nombre.
    Se usa para evitar colisiones entre sesiones concurrentes.

Estrategia subprocess-per-command:
  - Cada operacion (navigate, snapshot, click, fill) lanza un proceso corto.
  - El estado pesado (browser/CDP) vive en el daemon — el spawn del CLI es barato.
  - `--json` en snapshot para parsing estructurado cuando esta disponible.
  - Timeout Python por encima del timeout CLI por defecto (25 s).

Seguridad (trust boundary):
  - Ningun string provisto por el modelo o la pagina se interpola en un shell.
  - Todos los argumentos se pasan como elementos discretos de argv.
  - `shell=False` siempre.

Nota sobre el Containerfile:
  # La instalacion del binario agent-browser es responsabilidad del Containerfile:
  #   RUN npm install -g agent-browser && agent-browser install
  # NO se incluye como dependencia Python. Este modulo es puramente opcional.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

from hermes.security.browser_jail import (
    BrowserLauncherRequired,
    _jail_enabled,
    build_jailed_argv,
    build_jail_env,
    push_egress_policy,
)
from hermes.security.browser_launcher_client import (
    BrowserLauncherClient,
    BrowserLauncherUnavailable,
    BrowserLauncherError,
)

logger = logging.getLogger(__name__)

_BINARY = "agent-browser"
_CLI_TIMEOUT_S = 30.0  # default CLI op timeout is 25 s; we wrap at 30 s
_SUBPROCESS_TIMEOUT_S = _CLI_TIMEOUT_S + 5.0


class AgentBrowserNotInstalledError(RuntimeError):
    """El binario `agent-browser` no esta instalado o no esta en PATH.

    Instala con:
        npm install -g agent-browser && agent-browser install
    El Containerfile debe ejecutar estos comandos — no es una dep Python.
    """


class AgentBrowserCommandError(RuntimeError):
    """El binario agent-browser devolvio un codigo de salida no-cero."""


class AgentBrowserCli:
    """Adaptador real sobre el binario Rust agent-browser.

    Implementa AgentBrowserCliPort. Los tests inyectan FakeAgentBrowserCli.

    Construccion:
        cli = AgentBrowserCli(session_name="hermes-ent-123")
        await cli.start()   # verifica binario, arranca daemon si no corre

    Cierre:
        await cli.close()   # envia `close --all` al daemon de esta sesion
    """

    def __init__(
        self,
        *,
        session_name: str = "hermes-default",
        binary: str = _BINARY,
        subprocess_timeout_s: float = _SUBPROCESS_TIMEOUT_S,
        domains_whitelist: tuple[str, ...] = (),
        teaching_mode: bool = False,
    ) -> None:
        self._session_name = session_name
        self._binary = binary
        self._subprocess_timeout_s = subprocess_timeout_s
        self._domains_whitelist = domains_whitelist
        self._teaching_mode = teaching_mode
        self._started = False
        # Tracks whether the daemon process has been spawned through the jail.
        # The jail wraps the FIRST spawn (which boots the agent-browser daemon).
        # Subsequent CLI calls reuse the running daemon without re-wrapping.
        self._daemon_spawned = False
        self._launcher_client = BrowserLauncherClient()

    async def start(self) -> None:
        """Verifica que el binario esta en PATH.

        Raises:
            AgentBrowserNotInstalledError: si el binario no se encuentra.
        """
        if shutil.which(self._binary) is None:
            raise AgentBrowserNotInstalledError(
                f"El binario '{self._binary}' no esta en PATH. "
                "Instala con:\n    npm install -g agent-browser && agent-browser install\n"
                "Consulta el Containerfile del proyecto para la configuracion oficial."
            )
        self._started = True
        logger.info(
            "hermes.browser.agent_browser_cli_started",
            extra={"session": self._session_name, "binary": self._binary},
        )
        # Push egress policy to the proxy on session open, before the first
        # navigation. If the proxy socket is absent (CI / dev) this is a no-op.
        push_egress_policy(
            session_name=self._session_name,
            domains_whitelist=self._domains_whitelist,
            teaching_mode=self._teaching_mode,
        )

    async def navigate(self, url: str) -> None:
        await self._run(["open", url])

    async def snapshot(self) -> str:
        """Toma snapshot del accessibility tree (-i = interactive only)."""
        stdout, _ = await self._run(["snapshot", "-i"])
        return stdout

    async def click(self, ref: str) -> None:
        await self._run(["click", ref])

    async def type_(self, ref: str, text: str) -> None:
        await self._run(["fill", ref, text])

    async def current_url(self) -> str:
        """Extrae la URL del encabezado del snapshot (URL: <url>)."""
        snapshot_text = await self.snapshot()
        for line in snapshot_text.splitlines():
            if line.startswith("URL:"):
                return line[4:].strip()
        return ""

    async def close(self) -> None:
        if not self._started:
            return
        try:
            await self._run(["close", "--all"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.agent_browser_cli_close_failed",
                extra={"session": self._session_name, "error": str(exc)},
            )
        self._started = False

    # ------------------------------------------------------------------
    # Subprocess helper
    # ------------------------------------------------------------------

    async def _spawn_daemon_jailed(self) -> None:
        """Boot the agent-browser daemon inside the confinement jail.

        When HERMES_BROWSER_JAIL=1: sends a launch request to the root helper
        (BrowserLauncherClient). Hard-fails if the launcher is unavailable —
        there is NO bare-argv fallback when the jail is active.

        When HERMES_BROWSER_JAIL=0 (CI): no-op — the first browser_argv call
        runs unconfined directly via asyncio.create_subprocess_exec.
        """
        if not _jail_enabled():
            # CI path — no jail, no launcher needed.
            return

        has_credentials = bool(self._domains_whitelist)
        try:
            await self._launcher_client.launch(
                session_name=self._session_name,
                domains_whitelist=self._domains_whitelist,
            )
        except (BrowserLauncherUnavailable, BrowserLauncherError) as exc:
            # INVARIANT: no bare-argv fallback when jail is active.
            raise AgentBrowserCommandError(
                f"browser_jail: HERMES_BROWSER_JAIL=1 but launcher unavailable "
                f"for session={self._session_name!r}. "
                "Cannot run browser unconfined. "
                f"Launcher error: {exc}"
            ) from exc

        logger.info(
            "hermes.browser.agent_browser_daemon_jailed session=%s has_credentials=%s",
            self._session_name,
            has_credentials,
        )

    async def _run(self, args: list[str]) -> tuple[str, str]:
        """Lanza el CLI con los args dados y devuelve (stdout, stderr).

        El PRIMER llamado (daemon spawn) envuelve el argv con browser_jail
        si HERMES_BROWSER_JAIL=1. Las invocaciones siguientes reutilizan el
        daemon ya corriendo — no necesitan rewrap (son subcomandos cortos).

        Siempre pasa --session para aislamiento. shell=False obligatorio.

        Raises:
            AgentBrowserCommandError: si el proceso sale con codigo != 0.
        """
        browser_argv = [self._binary, "--session", self._session_name, *args]

        if not self._daemon_spawned:
            await self._spawn_daemon_jailed()
            self._daemon_spawned = True
            cmd = browser_argv
        else:
            cmd = browser_argv

        logger.debug(
            "hermes.browser.agent_browser_cli_run",
            extra={"cmd": cmd},
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._subprocess_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise AgentBrowserCommandError(
                f"agent-browser timeout after {self._subprocess_timeout_s}s: {cmd}"
            ) from exc

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            logger.warning(
                "hermes.browser.agent_browser_cli_nonzero",
                extra={
                    "cmd": cmd,
                    "returncode": proc.returncode,
                    "stderr": stderr[:500],
                },
            )
            raise AgentBrowserCommandError(
                f"agent-browser exited {proc.returncode}: {stderr[:200]}"
            )

        return stdout, stderr


def _parse_snapshot_json(raw: str) -> Any:
    """Intenta parsear JSON del output de snapshot --json.

    Devuelve el objeto parseado o None si raw no es JSON valido.
    El schema exacto del JSON de agent-browser no esta documentado publicamente
    (ver research: 'capture one real --json payload locally and pin to that').
    Solo se usa si el consumer solicita parsing estructurado.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
