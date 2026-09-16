"""Ports (Protocols) the application layer depends on. Infrastructure
implements them; tests provide in-memory fakes — the application layer never
imports a concrete infrastructure class."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TailnetPeer:
    name: str
    online: bool


@dataclass(frozen=True, slots=True)
class TailnetStatus:
    """Mirrors the ops lane's `/run/hermes/tailscale/status.json` contract:
    `{node_name, magicdns_suffix, online, peers:[{name, online}]}`."""

    node_name: str
    magicdns_suffix: str
    online: bool
    peers: tuple[TailnetPeer, ...] = field(default_factory=tuple)


class TailnetDirectoryPort(Protocol):
    def read(self) -> TailnetStatus:
        """Return the current tailnet status. Raises
        `TailnetDirectoryUnavailableError` if the status file is missing,
        unreadable, or malformed — fail-closed, never a stale/partial guess."""
        ...


class HostAllowlistPort(Protocol):
    """The daemon's persistent per-host SSH grant store."""

    def is_allowed(self, host: str) -> bool: ...

    def allow(self, host: str) -> None:
        """Idempotent: granting an already-allowed host is a no-op."""
        ...

    def list_allowed(self) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class SshExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int


class SshExecutorPort(Protocol):
    def run(
        self,
        *,
        host: str,
        command: str,
        timeout_s: int,
        stdin: str | None = None,
        max_output_bytes: int | None = None,
    ) -> SshExecutionResult:
        """Run `command` on `host` non-interactively. Raises
        `RemoteCommandTimeoutError` on timeout, `SshExecutionError` if the
        `ssh` binary cannot be spawned. Never raises on a non-zero remote
        exit code — that is a normal `SshExecutionResult.exit_code`."""
        ...


class AuditPort(Protocol):
    def record_ssh_call(
        self,
        *,
        host: str,
        command: str,
        exit_code: int,
        duration_ms: int,
        truncated: bool,
    ) -> None:
        """WORM audit entry. `command` is recorded in full (it is not
        secret); output BODIES never cross this port — only the truncated
        flag. Must never raise (audit failure must not crash the call whose
        result the owner is waiting on) — implementations fail-soft
        internally and log."""
        ...
