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
class HostGrant:
    """What the owner's allow-list knows about one host's SSH grant
    (spec 002 US3, D-4 — governed tailnet SSH capability ceiling).

    `capabilities` is None for a LOCAL (non-cloud) or unrecognised host —
    NO ceiling, today's unrestricted behaviour: the per-host HITL gate in
    `security_hook` is the only floor, unchanged. A CLOUD-managed grant
    (`managed_by == "cloud"`) always carries a non-empty `identity` and a
    non-empty `capabilities` set — the CEILING of what the use cases may do
    as that host. The use cases never widen it; they only ever check
    membership (`capability in capabilities`) before spawning `ssh`.
    """

    managed_by: str | None
    identity: str | None
    capabilities: frozenset[str] | None


class HostGrantPort(Protocol):
    def grant_for(self, host: str) -> HostGrant | None:
        """Metadata for `host` (canonical form, already tailnet-resolved).

        None when `host` is not on the allow-list at all — treated
        IDENTICALLY to a local grant (no ceiling) by the capability-ceiling
        check: membership/authorization is `security_hook`'s job alone: this
        port exists ONLY to discover a ceiling on TOP of an already-
        authorized call, never to re-decide whether the call is authorized.
        """
        ...


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
        user: str | None = None,
    ) -> SshExecutionResult:
        """Run `command` on `host` non-interactively. Raises
        `RemoteCommandTimeoutError` on timeout, `SshExecutionError` if the
        `ssh` binary cannot be spawned. Never raises on a non-zero remote
        exit code — that is a normal `SshExecutionResult.exit_code`.

        `user` (spec 002 US3, D-4): the remote login identity to force via
        `ssh -l`. None (the default) preserves today's behaviour exactly —
        no `-l` flag, the ambient/default remote user. Only ever non-None
        for a CLOUD-managed host, where it is the grant's declared
        `identity` — the use case computes it, never the caller/agent."""
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
        """WORM audit entry for an EXECUTED call. `command` is recorded in
        full (it is not secret); output BODIES never cross this port — only
        the truncated flag. Must never raise (audit failure must not crash
        the call whose result the owner is waiting on) — implementations
        fail-soft internally and log."""
        ...

    def record_ssh_denied(
        self,
        *,
        host: str,
        capability: str,
        identity: str,
        reason: str,
    ) -> None:
        """WORM audit entry for a capability-ceiling DENIAL (spec 002 US3,
        D-4) — the ssh subprocess was NEVER spawned. `capability` is the
        operation that was denied ("exec"/"file_read"/"file_write");
        `identity` is the grant's declared remote identity (never a
        caller-supplied one — there is none); `reason` is a short machine
        code (e.g. "capability_denied"). Must never raise — same fail-soft
        contract as `record_ssh_call`."""
        ...
