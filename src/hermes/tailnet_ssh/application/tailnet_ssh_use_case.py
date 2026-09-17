"""TailnetSshUseCase — the governed `tailnet_ssh` tool's execution.

Trust boundary note (mirrors `DelegationSurfaceAdapter`'s documented model):
by the time `execute()` runs, `hermes.runtime.security_hook`'s pre-tool-call
hook has ALREADY resolved the per-host HITL decision (block-and-resume card,
persistent allow-list) — this use case performs ZERO additional HOST
authorization of its own; duplicating that check here would risk drifting
from the single choke-point. It DOES still validate the host/command/timeout
shape (correctness, not authorization) and re-resolve the host against the
live directory, because the raw `host` the LLM passed must never be trusted
verbatim for the actual `ssh` invocation.

Capability ceiling (spec 002 US3, D-4): a SEPARATE, ADDITIVE check on top of
the above — a CLOUD-managed host's governed-SSH grant caps WHAT the agent
may do (here: "exec") and WHO it becomes (the grant's declared `identity`,
forced via `ssh -l`). Local/unmanaged hosts are entirely unaffected — no
ceiling, no forced identity, today's behaviour byte-for-byte. See
`hermes.tailnet_ssh.application.capability_ceiling.resolve_execution_
identity` for the single shared decision this and the two file use cases
all delegate to.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.tailnet_ssh.application.capability_ceiling import resolve_execution_identity
from hermes.tailnet_ssh.application.host_resolution import resolve_host
from hermes.tailnet_ssh.application.ports import (
    AuditPort,
    HostGrantPort,
    SshExecutorPort,
    TailnetDirectoryPort,
)
from hermes.tailnet_ssh.domain.command import RemoteCommand
from hermes.tailnet_ssh.domain.errors import RemoteCommandRejectedError, SshCapabilityDeniedError
from hermes.tailnet_ssh.domain.limits import MAX_STDIN_BYTES, validate_timeout

_CAPABILITY_EXEC = "exec"


@dataclass(frozen=True, slots=True)
class TailnetSshRequest:
    host: str
    command: str
    timeout_s: int
    stdin: str | None = None


@dataclass(frozen=True, slots=True)
class TailnetSshResult:
    host: str
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int


class TailnetSshUseCase:
    def __init__(
        self,
        *,
        directory: TailnetDirectoryPort,
        executor: SshExecutorPort,
        audit: AuditPort,
        host_grants: HostGrantPort | None = None,
    ) -> None:
        self._directory = directory
        self._executor = executor
        self._audit = audit
        self._host_grants = host_grants

    def execute(self, request: TailnetSshRequest) -> TailnetSshResult:
        command = RemoteCommand.parse(request.command)
        timeout_s = validate_timeout(request.timeout_s)
        stdin = _validate_stdin(request.stdin)
        host = resolve_host(request.host, self._directory.read())

        grant = self._host_grants.grant_for(host.value) if self._host_grants else None
        try:
            identity = resolve_execution_identity(
                grant, host=host.value, capability=_CAPABILITY_EXEC
            )
        except SshCapabilityDeniedError:
            self._audit.record_ssh_denied(
                host=host.value,
                capability=_CAPABILITY_EXEC,
                identity=(grant.identity if grant else None) or "",
                reason="capability_denied",
            )
            raise

        outcome = self._executor.run(
            host=host.value,
            command=command.value,
            timeout_s=timeout_s,
            stdin=stdin,
            user=identity,
        )

        self._audit.record_ssh_call(
            host=host.value,
            command=command.value,
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            truncated=outcome.stdout_truncated or outcome.stderr_truncated,
        )

        return TailnetSshResult(
            host=host.value,
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            stdout_truncated=outcome.stdout_truncated,
            stderr_truncated=outcome.stderr_truncated,
            duration_ms=outcome.duration_ms,
        )


def _validate_stdin(stdin: str | None) -> str | None:
    if stdin is None:
        return None
    if not isinstance(stdin, str):
        raise RemoteCommandRejectedError(
            f"stdin debe ser texto, recibido {type(stdin).__name__}"
        )
    if len(stdin.encode("utf-8", "replace")) > MAX_STDIN_BYTES:
        raise RemoteCommandRejectedError(f"stdin excede {MAX_STDIN_BYTES} bytes")
    return stdin
