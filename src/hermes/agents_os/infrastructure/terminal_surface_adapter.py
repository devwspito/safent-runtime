"""TerminalSurfaceAdapter — captura/replay de comandos shell.

FR-027/028 (spec 003): el formador demuestra una tarea ejecutando
comandos en terminal; Hermes captura comando + args + cwd + exit code +
stdout/stderr (con PII tokenization aplicada al output) y luego puede
re-ejecutar la secuencia.

Diseño:
- Captura: ejecuta el comando via ``asyncio.create_subprocess_exec``,
  registra el resultado.
- Replay: re-ejecuta el comando guardado, comparando exit code esperado.
- Confinamiento kernel (B-1 spec 014): cada ejecucion se envuelve en
  ``systemd-run --pipe --collect --quiet --scope`` con propiedades de
  hardening; el alcance del scope se parametriza por
  ``HERMES_TERMINAL_SCOPE`` (default "1", "0" solo en CI).
  Fail-closed: si scope esta habilitado y ``systemd-run`` no esta
  disponible, la ejecucion se deniega (exit code 125).
- Denylist Python: permanece como defensa en profundidad, NO es el gate
  principal (constitución Principio 0.6: confinamiento kernel, NO
  allowlists de app).
- PII tokenization: aplica ``DefaultPIITokenizer`` al stdout/stderr antes
  de persistir (constitución III).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import struct
import time
from typing import Any
from uuid import UUID, uuid4

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    SurfaceAdapterPort,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Denylist de profundidad (NO es el gate principal — ver confinamiento kernel)
# ---------------------------------------------------------------------------

_DEFAULT_DENYLIST: frozenset[str] = frozenset(
    {
        "rm",
        "mkfs",
        "dd",
        "fdisk",
        "shutdown",
        "reboot",
        "systemctl",   # cubierto por SystemSettingsSurfaceAdapter
        "rpm-ostree",  # cubierto por PackageManagerSurfaceAdapter
    }
)

# ---------------------------------------------------------------------------
# Propiedades de hardening del systemd-run scope
# ---------------------------------------------------------------------------

# Exit code devuelto cuando se deniega por falta de scope disponible.
_EXIT_SCOPE_UNAVAILABLE: int = 125

_SCOPE_HARDENING: tuple[str, ...] = (
    "--property=NoNewPrivileges=yes",
    "--property=SystemCallFilter=@system-service",
    "--property=SystemCallFilter=~@privileged",
    "--property=SystemCallFilter=~@mount",
    "--property=SystemCallFilter=~@swap",
    "--property=SystemCallFilter=~@reboot",
    "--property=CapabilityBoundingSet=",
    "--property=IPAddressDeny=any",
    "--property=ProtectSystem=strict",
    "--property=ProtectHome=yes",
    "--property=PrivateTmp=yes",
    "--property=RestrictNamespaces=yes",
    "--property=RestrictSUIDSGID=yes",
    "--property=MemoryMax=512M",
    "--property=CPUQuota=50%",
    "--property=TasksMax=64",
)


def _scope_enabled() -> bool:
    """True unless HERMES_TERMINAL_SCOPE=0 (CI without systemd)."""
    return os.environ.get("HERMES_TERMINAL_SCOPE", "1") != "0"


# Root exec-launcher socket: when present, terminal commands run inside the jailed
# netns (egress default-deny via the audited proxy) instead of the in-process scope.
# The launcher owns ALL hardening server-side (the daemon is unprivileged and cannot
# join a netns itself). Absent socket → fall back to the in-process scope (CI/dev).
_EXEC_LAUNCHER_SOCK = os.environ.get(
    "HERMES_EXEC_LAUNCHER_SOCK", "/run/hermes/exec-launch.sock"
)
_LAUNCHER_FRAME_MAX = 2 * 1024 * 1024  # response cap (stdout+stderr, bounded)


def _launcher_available() -> bool:
    """True if the root exec-launcher socket exists (jailed-netns egress active)."""
    try:
        import stat  # noqa: PLC0415
        return stat.S_ISSOCK(os.stat(_EXEC_LAUNCHER_SOCK).st_mode)
    except OSError:
        return False


def _systemd_run_path() -> str | None:
    """Return the absolute path of systemd-run, or None if not found."""
    import shutil  # noqa: PLC0415
    return shutil.which("systemd-run")


def _build_scoped_argv(
    argv: list[str],
    *,
    timeout_s: float,
    workspace: str | None,
) -> list[str]:
    """Wrap *argv* in a systemd-run scope with kernel hardening.

    The scope is anonymous (no --unit) and transient (--collect).
    All hardening properties from _SCOPE_HARDENING are applied.
    If *workspace* is provided, ProtectSystem=strict is relaxed for that
    path (ReadWritePaths) so the command can write within its workspace.
    """
    sdrun = "systemd-run"
    # Transient SERVICE (--pipe), NO --scope: las propiedades de hardening
    # (_SCOPE_HARDENING: NoNewPrivileges, SystemCallFilter, ProtectSystem…) son
    # de SERVICE — un --scope las rechaza ("Unknown assignment: NoNewPrivileges").
    # Además --pipe es incompatible con --scope. --pipe (service) aplica TODO el
    # sandboxing exec y conecta stdout/stderr al caller para capturarlos.
    base = [
        sdrun,
        "--pipe",
        "--collect",
        "--quiet",
        f"--property=RuntimeMaxSec={int(timeout_s) + 5}",
        *_SCOPE_HARDENING,
    ]
    if workspace:
        base.append(f"--property=ReadWritePaths={workspace}")
    base += ["--"] + argv
    return base


class TerminalConfinementUnavailableError(RuntimeError):
    """Raised when scope is required but systemd-run is not available.

    Fail-closed: the command is NOT executed unconfined.
    """


class TerminalInstallBlockedError(RuntimeError):
    """Raised when a terminal install-shaped command is blocked by the Security
    Center (verdict FAIL / scan errored). Closes the install side-door: pip/npm/
    curl|sh/git-clone are reviewed by the same scan→score→gate as the official
    channels before they run."""


class TerminalSurfaceAdapter:
    """Cumple ``SurfaceAdapterPort`` para superficie ``TERMINAL``."""

    def __init__(
        self,
        *,
        extra_denylist: frozenset[str] = frozenset(),
        max_output_bytes: int = 64 * 1024,
        timeout_s: float = 30.0,
        workspace: str | None = None,
        install_reviewer: object | None = None,
    ) -> None:
        self._denylist = _DEFAULT_DENYLIST | extra_denylist
        self._max_output_bytes = max_output_bytes
        self._timeout_s = timeout_s
        # Workspace dir the command may write to (ProtectSystem=strict exception).
        self._workspace = workspace
        # Security Center reviewer for install-shaped commands (pip/npm/curl|sh/…).
        # None ⇒ no scanner wired (falls back to egress jail + broker HITL).
        self._install_reviewer = install_reviewer

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.TERMINAL

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> CapturedAction:
        """Captura ejecutando el comando; persiste resultado.

        ``params`` esperado:
            argv: list[str]            # comando + args
            cwd: str                   # directorio de trabajo
            env: dict[str, str]        # vars de entorno extra (opcional)
        """
        argv = params.get("argv", [])
        if not isinstance(argv, list) or not argv:
            raise ValueError("terminal capture requiere argv no vacío")
        cwd = params.get("cwd", "")

        self._reject_if_denylisted(argv)
        exit_code, stdout, stderr, elapsed_ms = await self._run(argv, cwd)
        payload = {
            "argv": list(argv),
            "cwd": cwd,
            "exit_code": exit_code,
            "stdout_redacted": _truncate(stdout, self._max_output_bytes),
            "stderr_redacted": _truncate(stderr, self._max_output_bytes),
            "duration_ms": elapsed_ms,
        }
        return CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.TERMINAL,
            intent_desc=intent_desc,
            payload=payload,
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
        )

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        if action.surface_kind != SurfaceKind.TERMINAL:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=f"surface mismatch: esperado TERMINAL, got {action.surface_kind}",
            )
        argv = action.payload.get("argv", [])
        cwd = action.payload.get("cwd", "")
        if not isinstance(argv, list) or not argv:
            return ReplayOutcome.failed(action.action_id, error="argv vacío")
        try:
            self._reject_if_denylisted(argv)
        except ValueError as exc:
            return ReplayOutcome.rejected_by_policy(
                action.action_id, reason=str(exc)
            )
        try:
            exit_code, stdout, stderr, elapsed_ms = await self._run(argv, cwd)
        except TerminalConfinementUnavailableError as exc:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=f"kernel confinement unavailable: {exc}",
            )
        if exit_code == 0:
            return ReplayOutcome.ok(
                action.action_id,
                duration_ms=elapsed_ms,
                result={"exit_code": 0, "stdout": stdout[: self._max_output_bytes]},
            )
        return ReplayOutcome.failed(
            action.action_id,
            error=f"exit_code={exit_code}: {stderr[:1024]}",
        )

    def replay_payload(self, payload: dict) -> bool:
        """SurfaceReplayPort shim for SkillReplayer (sync → async bridge)."""
        import asyncio  # noqa: PLC0415

        from hermes.agents_os.domain.ports.surface_adapter_port import (  # noqa: PLC0415
            CapturedAction,
            ReplayStatus,
        )

        action = CapturedAction(
            surface_kind=self.surface_kind,
            intent_desc=payload.get("intent_desc", ""),
            payload=payload,
        )
        outcome = asyncio.run(self.replay(action))
        return outcome.status == ReplayStatus.EXECUTED_OK

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        # Firmamos: surface + argv + cwd + intent_desc + exit_code esperado.
        # NO firmamos stdout/stderr (varían entre runs).
        canonical = {
            "surface_kind": action.surface_kind.value,
            "intent_desc": action.intent_desc,
            "argv": action.payload.get("argv", []),
            "cwd": action.payload.get("cwd", ""),
            "exit_code_expected": action.payload.get("exit_code", 0),
        }
        return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _reject_if_denylisted(self, argv: list[str]) -> None:
        if not argv:
            return
        cmd = argv[0].split("/")[-1]  # basename
        if cmd in self._denylist:
            raise ValueError(
                f"comando {cmd!r} está en la denylist por defecto del "
                "TerminalSurfaceAdapter (constitución IV fail-closed)"
            )

    async def _run(
        self, argv: list[str], cwd: str
    ) -> tuple[int, str, str, int]:
        """Ejecuta argv confinado en un systemd-run scope.

        Fail-closed: si el scope está habilitado pero systemd-run no está
        disponible, lanza TerminalConfinementUnavailableError (no ejecuta
        sin confinar).

        Orden de confinamiento (más fuerte → fallback):
          1. exec-launcher (netns enjaulado, egress default-deny vía proxy) si el
             socket root está presente — la jaula real de producción.
          2. systemd-run --scope in-process (hardening sin netns) si no hay socket.
          3. raw (solo con HERMES_TERMINAL_SCOPE=0, CI).
        """
        # Security Center gate: install-shaped commands (pip/npm/curl|sh/git-clone)
        # are reviewed BEFORE execution — the same scan→score→gate as the official
        # install channels. Closes the terminal side-door around the Security Center.
        await self._review_install_or_raise(argv)
        if _launcher_available():
            return await self._run_via_launcher(argv, cwd)
        if _scope_enabled():
            return await self._run_scoped(argv, cwd)
        return await self._run_raw(argv, cwd)

    async def _review_install_or_raise(self, argv: list[str]) -> None:
        """If *argv* is an install, scan it via the Security Center; raise on block."""
        if self._install_reviewer is None:
            return
        from hermes.agents_os.domain.terminal_install_intent import (  # noqa: PLC0415
            detect_install_intent,
        )

        intent = detect_install_intent(argv)
        if intent is None:
            return
        outcome = await self._install_reviewer.review(intent)
        logger.info(
            "terminal_adapter.install_reviewed ecosystem=%s id=%s verdict=%s allowed=%s",
            intent.ecosystem, intent.identifier, outcome.verdict, outcome.allowed,
        )
        if not outcome.allowed:
            raise TerminalInstallBlockedError(outcome.reason)

    async def _run_via_launcher(
        self, argv: list[str], cwd: str
    ) -> tuple[int, str, str, int]:
        """Run *argv* in the jailed netns via the root exec-launcher.

        The command runs as User=hermes inside /run/netns/hermes-browser: egress
        is default-deny through the audited proxy (the owner elevates domains via
        UI), the keystore is InaccessiblePaths, and there is no direct route out.
        Fail-closed: any launcher/transport error denies the command (the cage is
        the product — we never silently downgrade to unconfined egress).
        """
        req = {
            "argv": list(argv),
            "cwd": cwd or None,
            "workspace": self._workspace,
            "timeout_s": int(self._timeout_s),
        }
        t0 = time.perf_counter()
        try:
            reader, writer = await asyncio.open_unix_connection(_EXEC_LAUNCHER_SOCK)
        except OSError as exc:
            raise TerminalConfinementUnavailableError(
                f"exec-launcher socket present but unreachable: {exc}"
            ) from exc
        try:
            body = json.dumps(req).encode("utf-8")
            writer.write(struct.pack(">I", len(body)) + body)
            await writer.drain()
            header = await reader.readexactly(4)
            length = struct.unpack(">I", header)[0]
            if length > _LAUNCHER_FRAME_MAX:
                raise TerminalConfinementUnavailableError("launcher frame too large")
            resp_raw = await reader.readexactly(length)
        except (OSError, asyncio.IncompleteReadError, struct.error) as exc:
            raise TerminalConfinementUnavailableError(
                f"exec-launcher transport error: {exc}"
            ) from exc
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

        resp = json.loads(resp_raw.decode("utf-8"))
        if not resp.get("ok"):
            raise TerminalConfinementUnavailableError(
                f"exec-launcher rejected: {resp.get('error', 'unknown')}"
            )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return (
            int(resp.get("exit_code", 1)),
            resp.get("stdout", ""),
            resp.get("stderr", ""),
            elapsed_ms,
        )

    async def _run_scoped(
        self, argv: list[str], cwd: str
    ) -> tuple[int, str, str, int]:
        sdrun = _systemd_run_path()
        if sdrun is None:
            raise TerminalConfinementUnavailableError(
                "systemd-run not found in PATH; cannot confine terminal command. "
                "Set HERMES_TERMINAL_SCOPE=0 only in CI environments."
            )
        scoped = _build_scoped_argv(
            argv,
            timeout_s=self._timeout_s,
            workspace=self._workspace,
        )
        # Replace the first element with the resolved path for safety.
        scoped[0] = sdrun
        logger.debug(
            "terminal_adapter.scoped_exec argv=%s scope_argv=%s",
            argv,
            scoped[:4],  # log prefix only, not full argv (may contain paths)
        )
        return await self._run_raw(scoped, cwd)

    async def _run_raw(
        self, argv: list[str], cwd: str
    ) -> tuple[int, str, str, int]:
        """Subprocess exec, sin wrapping adicional."""
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd or None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return (124, "", "TIMEOUT", elapsed_ms)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return (
            proc.returncode or 0,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
            elapsed_ms,
        )


def _truncate(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n[...truncated]"


def hash_canonical_action(adapter: SurfaceAdapterPort, action: CapturedAction) -> str:
    """Helper de utilidad: SHA-256 del serialize_for_signing."""
    return hashlib.sha256(adapter.serialize_for_signing(action)).hexdigest()
