"""TailnetFileGetUseCase / TailnetFilePutUseCase — small file transfer over
the same governed SSH channel, no scp/sftp binary needed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from hermes.tailnet_ssh.application.ports import (
    HostGrant,
    SshExecutionResult,
    TailnetPeer,
    TailnetStatus,
)
from hermes.tailnet_ssh.application.tailnet_file_use_case import (
    TailnetFileGetRequest,
    TailnetFileGetUseCase,
    TailnetFilePutRequest,
    TailnetFilePutUseCase,
)
from hermes.tailnet_ssh.domain.errors import (
    RemoteCommandRejectedError,
    RemotePathRejectedError,
    SshCapabilityDeniedError,
)
from hermes.tailnet_ssh.domain.limits import MAX_FILE_BYTES

pytestmark = pytest.mark.unit

_STATUS = TailnetStatus(
    node_name="safent-agent", magicdns_suffix="tailxxxx.ts.net", online=True,
    peers=(TailnetPeer(name="db1", online=True),),
)


class FakeDirectory:
    def read(self) -> TailnetStatus:
        return _STATUS


@dataclass
class FakeExecutor:
    result: SshExecutionResult
    calls: list[dict[str, Any]] = field(default_factory=list)

    def run(self, *, host, command, timeout_s, stdin=None, max_output_bytes=None, user=None):
        self.calls.append({
            "host": host, "command": command, "timeout_s": timeout_s,
            "stdin": stdin, "max_output_bytes": max_output_bytes, "user": user,
        })
        return self.result


@dataclass
class FakeAudit:
    calls: list[dict[str, Any]] = field(default_factory=list)
    denied_calls: list[dict[str, Any]] = field(default_factory=list)

    def record_ssh_call(self, *, host, command, exit_code, duration_ms, truncated):
        self.calls.append({"host": host, "command": command, "exit_code": exit_code,
                            "duration_ms": duration_ms, "truncated": truncated})

    def record_ssh_denied(self, *, host, capability, identity, reason):
        self.denied_calls.append({
            "host": host, "capability": capability, "identity": identity, "reason": reason,
        })


@dataclass
class FakeHostGrants:
    grants: dict[str, HostGrant] = field(default_factory=dict)

    def grant_for(self, host: str) -> HostGrant | None:
        return self.grants.get(host)


class TestTailnetFileGetUseCase:
    def test_builds_cat_command_with_quoted_path(self) -> None:
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=0, stdout="contents", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=5,
        ))
        use_case = TailnetFileGetUseCase(
            directory=FakeDirectory(), executor=executor, audit=FakeAudit()
        )

        result = use_case.execute(TailnetFileGetRequest(host="db1", path="/etc/hostname"))

        assert result.content == "contents"
        assert executor.calls[0]["command"] == "cat -- /etc/hostname"
        assert executor.calls[0]["max_output_bytes"] == MAX_FILE_BYTES

    def test_shell_quotes_a_path_with_spaces(self) -> None:
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=0, stdout="", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=1,
        ))
        use_case = TailnetFileGetUseCase(
            directory=FakeDirectory(), executor=executor, audit=FakeAudit()
        )

        use_case.execute(TailnetFileGetRequest(host="db1", path="/tmp/a file; rm -rf /"))

        # shlex.quote wraps the whole path in single quotes — the embedded
        # `;` never breaks out as a second remote command.
        assert executor.calls[0]["command"] == "cat -- '/tmp/a file; rm -rf /'"

    def test_nonzero_exit_raises(self) -> None:
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=1, stdout="", stderr="No such file", stdout_truncated=False,
            stderr_truncated=False, duration_ms=1,
        ))
        use_case = TailnetFileGetUseCase(
            directory=FakeDirectory(), executor=executor, audit=FakeAudit()
        )

        with pytest.raises(RemoteCommandRejectedError):
            use_case.execute(TailnetFileGetRequest(host="db1", path="/missing"))

    def test_rejects_invalid_path_before_touching_executor(self) -> None:
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=0, stdout="", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=1,
        ))
        use_case = TailnetFileGetUseCase(
            directory=FakeDirectory(), executor=executor, audit=FakeAudit()
        )

        with pytest.raises(RemotePathRejectedError):
            use_case.execute(TailnetFileGetRequest(host="db1", path=""))
        assert executor.calls == []


class TestTailnetFilePutUseCase:
    def test_builds_cat_redirect_command_and_forwards_stdin(self) -> None:
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=0, stdout="", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=3,
        ))
        use_case = TailnetFilePutUseCase(
            directory=FakeDirectory(), executor=executor, audit=FakeAudit()
        )

        result = use_case.execute(
            TailnetFilePutRequest(host="db1", path="/tmp/out.txt", content="hello")
        )

        assert result.bytes_written == 5
        assert executor.calls[0]["command"] == "cat > /tmp/out.txt"
        assert executor.calls[0]["stdin"] == "hello"

    def test_rejects_oversized_content_before_touching_executor(self) -> None:
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=0, stdout="", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=1,
        ))
        use_case = TailnetFilePutUseCase(
            directory=FakeDirectory(), executor=executor, audit=FakeAudit()
        )

        with pytest.raises(RemoteCommandRejectedError):
            use_case.execute(TailnetFilePutRequest(
                host="db1", path="/tmp/x", content="x" * (MAX_FILE_BYTES + 1)
            ))
        assert executor.calls == []


class TestTailnetFileGetUseCaseCapabilityCeiling:
    """spec 002 US3, D-4 — file_read is the ceiling checked for GET."""

    def test_cloud_host_with_file_read_forces_declared_identity(self) -> None:
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=0, stdout="contents", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=5,
        ))
        host_grants = FakeHostGrants(grants={
            "db1.tailxxxx.ts.net": HostGrant(
                managed_by="cloud", identity="auditor", capabilities=frozenset({"file_read"})
            )
        })
        use_case = TailnetFileGetUseCase(
            directory=FakeDirectory(), executor=executor, audit=FakeAudit(), host_grants=host_grants
        )

        use_case.execute(TailnetFileGetRequest(host="db1", path="/etc/hostname"))

        assert executor.calls[0]["user"] == "auditor"

    def test_cloud_host_without_file_read_denies_before_executor(self) -> None:
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=0, stdout="", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=1,
        ))
        audit = FakeAudit()
        host_grants = FakeHostGrants(grants={
            "db1.tailxxxx.ts.net": HostGrant(
                managed_by="cloud", identity="deploy", capabilities=frozenset({"exec"})
            )
        })
        use_case = TailnetFileGetUseCase(
            directory=FakeDirectory(), executor=executor, audit=audit, host_grants=host_grants
        )

        with pytest.raises(SshCapabilityDeniedError):
            use_case.execute(TailnetFileGetRequest(host="db1", path="/etc/hostname"))

        assert executor.calls == []
        assert audit.denied_calls == [{
            "host": "db1.tailxxxx.ts.net", "capability": "file_read",
            "identity": "deploy", "reason": "capability_denied",
        }]

    def test_local_host_is_unaffected(self) -> None:
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=0, stdout="contents", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=5,
        ))
        use_case = TailnetFileGetUseCase(
            directory=FakeDirectory(), executor=executor, audit=FakeAudit()
        )

        use_case.execute(TailnetFileGetRequest(host="db1", path="/etc/hostname"))

        assert executor.calls[0]["user"] is None


class TestTailnetFilePutUseCaseCapabilityCeiling:
    """spec 002 US3, D-4 — file_write is the ceiling checked for PUT."""

    def test_cloud_host_with_file_write_forces_declared_identity(self) -> None:
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=0, stdout="", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=3,
        ))
        host_grants = FakeHostGrants(grants={
            "db1.tailxxxx.ts.net": HostGrant(
                managed_by="cloud", identity="deploy", capabilities=frozenset({"file_write"})
            )
        })
        use_case = TailnetFilePutUseCase(
            directory=FakeDirectory(), executor=executor, audit=FakeAudit(), host_grants=host_grants
        )

        use_case.execute(TailnetFilePutRequest(host="db1", path="/tmp/out.txt", content="hi"))

        assert executor.calls[0]["user"] == "deploy"

    def test_cloud_host_without_file_write_denies_before_executor(self) -> None:
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=0, stdout="", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=1,
        ))
        audit = FakeAudit()
        host_grants = FakeHostGrants(grants={
            "db1.tailxxxx.ts.net": HostGrant(
                managed_by="cloud", identity="auditor", capabilities=frozenset({"file_read"})
            )
        })
        use_case = TailnetFilePutUseCase(
            directory=FakeDirectory(), executor=executor, audit=audit, host_grants=host_grants
        )

        with pytest.raises(SshCapabilityDeniedError):
            use_case.execute(
                TailnetFilePutRequest(host="db1", path="/tmp/out.txt", content="hi")
            )

        assert executor.calls == []
        assert audit.denied_calls == [{
            "host": "db1.tailxxxx.ts.net", "capability": "file_write",
            "identity": "auditor", "reason": "capability_denied",
        }]

    def test_narrowed_grant_denies_a_capability_it_used_to_allow(self) -> None:
        """Re-applying a bundle that narrows capabilities takes effect
        immediately — the use case always reads the CURRENT grant, never a
        cached one."""
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=0, stdout="", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=1,
        ))
        host_grants = FakeHostGrants(grants={
            "db1.tailxxxx.ts.net": HostGrant(
                managed_by="cloud", identity="deploy",
                capabilities=frozenset({"exec", "file_write"}),
            )
        })
        use_case = TailnetFilePutUseCase(
            directory=FakeDirectory(), executor=executor, audit=FakeAudit(), host_grants=host_grants
        )
        use_case.execute(TailnetFilePutRequest(host="db1", path="/tmp/out.txt", content="hi"))
        assert len(executor.calls) == 1  # first call succeeded under the wider grant

        host_grants.grants["db1.tailxxxx.ts.net"] = HostGrant(
            managed_by="cloud", identity="deploy", capabilities=frozenset({"exec"})
        )

        with pytest.raises(SshCapabilityDeniedError):
            use_case.execute(TailnetFilePutRequest(host="db1", path="/tmp/out.txt", content="hi"))
        assert len(executor.calls) == 1  # the narrowed call never reached the executor

    def test_local_host_is_unaffected(self) -> None:
        executor = FakeExecutor(result=SshExecutionResult(
            exit_code=0, stdout="", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=3,
        ))
        use_case = TailnetFilePutUseCase(
            directory=FakeDirectory(), executor=executor, audit=FakeAudit()
        )

        use_case.execute(TailnetFilePutRequest(host="db1", path="/tmp/out.txt", content="hi"))

        assert executor.calls[0]["user"] is None
