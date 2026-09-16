"""TailnetFileGetUseCase / TailnetFilePutUseCase — small read/write file
transfer over the SAME governed SSH channel as tailnet_ssh (no scp/sftp
dependency: Safent builds the remote `cat` command itself and shell-quotes
the path, so no new binary or ProxyCommand wiring is needed).

Same trust boundary as TailnetSshUseCase: the per-host HITL decision is
already resolved by security_hook's pre-tool-call hook before `execute()`
runs.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from hermes.tailnet_ssh.application.host_resolution import resolve_host
from hermes.tailnet_ssh.application.ports import (
    AuditPort,
    SshExecutorPort,
    TailnetDirectoryPort,
)
from hermes.tailnet_ssh.domain.errors import RemoteCommandRejectedError
from hermes.tailnet_ssh.domain.limits import MAX_FILE_BYTES, MAX_TIMEOUT_S
from hermes.tailnet_ssh.domain.remote_path import RemotePath

_FILE_TRANSFER_TIMEOUT_S = min(60, MAX_TIMEOUT_S)


@dataclass(frozen=True, slots=True)
class TailnetFileGetRequest:
    host: str
    path: str


@dataclass(frozen=True, slots=True)
class TailnetFileGetResult:
    host: str
    path: str
    content: str
    truncated: bool
    duration_ms: int


class TailnetFileGetUseCase:
    def __init__(
        self,
        *,
        directory: TailnetDirectoryPort,
        executor: SshExecutorPort,
        audit: AuditPort,
    ) -> None:
        self._directory = directory
        self._executor = executor
        self._audit = audit

    def execute(self, request: TailnetFileGetRequest) -> TailnetFileGetResult:
        remote_path = RemotePath.parse(request.path)
        host = resolve_host(request.host, self._directory.read())
        command = f"cat -- {shlex.quote(remote_path.value)}"

        outcome = self._executor.run(
            host=host.value,
            command=command,
            timeout_s=_FILE_TRANSFER_TIMEOUT_S,
            max_output_bytes=MAX_FILE_BYTES,
        )
        if outcome.exit_code != 0:
            raise RemoteCommandRejectedError(
                f"tailnet_file_get: «cat» remoto falló (exit {outcome.exit_code}): "
                f"{outcome.stderr[:200]}"
            )

        self._audit.record_ssh_call(
            host=host.value,
            command=f"tailnet_file_get {remote_path.value}",
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            truncated=outcome.stdout_truncated,
        )
        return TailnetFileGetResult(
            host=host.value,
            path=remote_path.value,
            content=outcome.stdout,
            truncated=outcome.stdout_truncated,
            duration_ms=outcome.duration_ms,
        )


@dataclass(frozen=True, slots=True)
class TailnetFilePutRequest:
    host: str
    path: str
    content: str


@dataclass(frozen=True, slots=True)
class TailnetFilePutResult:
    host: str
    path: str
    bytes_written: int
    duration_ms: int


class TailnetFilePutUseCase:
    def __init__(
        self,
        *,
        directory: TailnetDirectoryPort,
        executor: SshExecutorPort,
        audit: AuditPort,
    ) -> None:
        self._directory = directory
        self._executor = executor
        self._audit = audit

    def execute(self, request: TailnetFilePutRequest) -> TailnetFilePutResult:
        remote_path = RemotePath.parse(request.path)
        content_bytes = _validate_content(request.content)
        host = resolve_host(request.host, self._directory.read())
        command = f"cat > {shlex.quote(remote_path.value)}"

        outcome = self._executor.run(
            host=host.value,
            command=command,
            timeout_s=_FILE_TRANSFER_TIMEOUT_S,
            stdin=request.content,
            max_output_bytes=MAX_FILE_BYTES,
        )
        if outcome.exit_code != 0:
            raise RemoteCommandRejectedError(
                f"tailnet_file_put: «cat» remoto falló (exit {outcome.exit_code}): "
                f"{outcome.stderr[:200]}"
            )

        self._audit.record_ssh_call(
            host=host.value,
            command=f"tailnet_file_put {remote_path.value} ({len(content_bytes)}B)",
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            truncated=False,
        )
        return TailnetFilePutResult(
            host=host.value,
            path=remote_path.value,
            bytes_written=len(content_bytes),
            duration_ms=outcome.duration_ms,
        )


def _validate_content(content: str) -> bytes:
    if not isinstance(content, str):
        raise RemoteCommandRejectedError(
            f"content debe ser texto, recibido {type(content).__name__}"
        )
    encoded = content.encode("utf-8", "replace")
    if len(encoded) > MAX_FILE_BYTES:
        raise RemoteCommandRejectedError(f"content excede {MAX_FILE_BYTES} bytes")
    return encoded
