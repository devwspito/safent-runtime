"""TailnetSshUseCase — orchestration tested against in-memory fakes only
(no subprocess, no filesystem, no network)."""

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
from hermes.tailnet_ssh.application.tailnet_ssh_use_case import (
    TailnetSshRequest,
    TailnetSshUseCase,
)
from hermes.tailnet_ssh.domain.errors import (
    InvalidTailnetHostError,
    RemoteCommandRejectedError,
    RemoteCommandTimeoutError,
    SshCapabilityDeniedError,
    UnknownTailnetHostError,
)

pytestmark = pytest.mark.unit

_STATUS = TailnetStatus(
    node_name="safent-agent",
    magicdns_suffix="tailxxxx.ts.net",
    online=True,
    peers=(TailnetPeer(name="db1", online=True),),
)


class FakeDirectory:
    def __init__(self, status: TailnetStatus = _STATUS) -> None:
        self.status = status

    def read(self) -> TailnetStatus:
        return self.status


@dataclass
class FakeExecutor:
    result: SshExecutionResult
    calls: list[dict[str, Any]] = field(default_factory=list)
    raise_error: Exception | None = None

    def run(self, *, host, command, timeout_s, stdin=None, max_output_bytes=None, user=None):
        self.calls.append({
            "host": host, "command": command, "timeout_s": timeout_s,
            "stdin": stdin, "max_output_bytes": max_output_bytes, "user": user,
        })
        if self.raise_error is not None:
            raise self.raise_error
        return self.result


@dataclass
class FakeAudit:
    calls: list[dict[str, Any]] = field(default_factory=list)
    denied_calls: list[dict[str, Any]] = field(default_factory=list)

    def record_ssh_call(self, *, host, command, exit_code, duration_ms, truncated):
        self.calls.append({
            "host": host, "command": command, "exit_code": exit_code,
            "duration_ms": duration_ms, "truncated": truncated,
        })

    def record_ssh_denied(self, *, host, capability, identity, reason):
        self.denied_calls.append({
            "host": host, "capability": capability, "identity": identity, "reason": reason,
        })


@dataclass
class FakeHostGrants:
    grants: dict[str, HostGrant] = field(default_factory=dict)

    def grant_for(self, host: str) -> HostGrant | None:
        return self.grants.get(host)


def _ok_result(**overrides: Any) -> SshExecutionResult:
    base = dict(
        exit_code=0, stdout="hi\n", stderr="", stdout_truncated=False,
        stderr_truncated=False, duration_ms=42,
    )
    base.update(overrides)
    return SshExecutionResult(**base)


class TestTailnetSshUseCaseHappyPath:
    def test_resolves_canonical_host_and_returns_result(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        audit = FakeAudit()
        use_case = TailnetSshUseCase(directory=directory, executor=executor, audit=audit)

        result = use_case.execute(
            TailnetSshRequest(host="db1", command="uptime", timeout_s=30)
        )

        assert result.host == "db1.tailxxxx.ts.net"
        assert result.exit_code == 0
        assert result.stdout == "hi\n"
        assert executor.calls[0]["host"] == "db1.tailxxxx.ts.net"
        assert executor.calls[0]["command"] == "uptime"
        assert executor.calls[0]["timeout_s"] == 30

    def test_passes_command_as_single_opaque_string_never_builds_a_shell_line(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        use_case = TailnetSshUseCase(directory=directory, executor=executor, audit=FakeAudit())
        dangerous = "echo hi && rm -rf /tmp/x; $(whoami)"

        use_case.execute(TailnetSshRequest(host="db1", command=dangerous, timeout_s=5))

        assert executor.calls[0]["command"] == dangerous  # untouched, single value

    def test_records_audit_with_host_command_exit_code_duration(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result(exit_code=1, duration_ms=777))
        audit = FakeAudit()
        use_case = TailnetSshUseCase(directory=directory, executor=executor, audit=audit)

        use_case.execute(TailnetSshRequest(host="db1", command="false", timeout_s=5))

        assert audit.calls == [{
            "host": "db1.tailxxxx.ts.net", "command": "false",
            "exit_code": 1, "duration_ms": 777, "truncated": False,
        }]

    def test_audit_truncated_flag_is_or_of_stdout_stderr(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result(stderr_truncated=True))
        audit = FakeAudit()
        use_case = TailnetSshUseCase(directory=directory, executor=executor, audit=audit)

        use_case.execute(TailnetSshRequest(host="db1", command="ls", timeout_s=5))

        assert audit.calls[0]["truncated"] is True

    def test_forwards_stdin_to_executor(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        use_case = TailnetSshUseCase(directory=directory, executor=executor, audit=FakeAudit())

        use_case.execute(
            TailnetSshRequest(host="db1", command="cat", timeout_s=5, stdin="payload")
        )

        assert executor.calls[0]["stdin"] == "payload"


class TestTailnetSshUseCaseValidationFailsBeforeExecution:
    def test_invalid_host_raises_and_never_touches_executor(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        use_case = TailnetSshUseCase(directory=directory, executor=executor, audit=FakeAudit())

        with pytest.raises(InvalidTailnetHostError):
            use_case.execute(
                TailnetSshRequest(host="100.64.1.2", command="ls", timeout_s=5)
            )
        assert executor.calls == []

    def test_unknown_host_raises_and_never_touches_executor(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        use_case = TailnetSshUseCase(directory=directory, executor=executor, audit=FakeAudit())

        with pytest.raises(UnknownTailnetHostError):
            use_case.execute(
                TailnetSshRequest(host="evil.com", command="ls", timeout_s=5)
            )
        assert executor.calls == []

    def test_out_of_range_timeout_raises_and_never_touches_executor(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        use_case = TailnetSshUseCase(directory=directory, executor=executor, audit=FakeAudit())

        with pytest.raises(RemoteCommandRejectedError):
            use_case.execute(TailnetSshRequest(host="db1", command="ls", timeout_s=301))
        assert executor.calls == []

    def test_empty_command_raises_and_never_touches_executor(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        use_case = TailnetSshUseCase(directory=directory, executor=executor, audit=FakeAudit())

        with pytest.raises(RemoteCommandRejectedError):
            use_case.execute(TailnetSshRequest(host="db1", command="   ", timeout_s=5))
        assert executor.calls == []

    def test_oversized_stdin_raises_and_never_touches_executor(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        use_case = TailnetSshUseCase(directory=directory, executor=executor, audit=FakeAudit())

        with pytest.raises(RemoteCommandRejectedError):
            use_case.execute(
                TailnetSshRequest(
                    host="db1", command="cat", timeout_s=5, stdin="x" * (1024 * 1024 + 1)
                )
            )
        assert executor.calls == []


class TestTailnetSshUseCasePropagatesExecutorErrors:
    def test_timeout_propagates_and_audit_not_called(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result(), raise_error=RemoteCommandTimeoutError("boom"))
        audit = FakeAudit()
        use_case = TailnetSshUseCase(directory=directory, executor=executor, audit=audit)

        with pytest.raises(RemoteCommandTimeoutError):
            use_case.execute(TailnetSshRequest(host="db1", command="sleep 999", timeout_s=1))
        assert audit.calls == []


class TestTailnetSshUseCaseCapabilityCeiling:
    """spec 002 US3, D-4 — governed-SSH capability ceiling for CLOUD-managed
    hosts; local/unmanaged hosts are entirely unaffected."""

    def test_cloud_host_with_exec_capability_forces_declared_identity(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        host_grants = FakeHostGrants(grants={
            "db1.tailxxxx.ts.net": HostGrant(
                managed_by="cloud", identity="deploy", capabilities=frozenset({"exec"})
            )
        })
        use_case = TailnetSshUseCase(
            directory=directory, executor=executor, audit=FakeAudit(), host_grants=host_grants
        )

        use_case.execute(TailnetSshRequest(host="db1", command="uptime", timeout_s=5))

        assert executor.calls[0]["user"] == "deploy"

    def test_cloud_host_without_exec_capability_denies_before_executor(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        audit = FakeAudit()
        host_grants = FakeHostGrants(grants={
            "db1.tailxxxx.ts.net": HostGrant(
                managed_by="cloud", identity="auditor", capabilities=frozenset({"file_read"})
            )
        })
        use_case = TailnetSshUseCase(
            directory=directory, executor=executor, audit=audit, host_grants=host_grants
        )

        with pytest.raises(SshCapabilityDeniedError):
            use_case.execute(TailnetSshRequest(host="db1", command="uptime", timeout_s=5))

        assert executor.calls == []

    def test_denial_is_audited_with_capability_and_identity(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        audit = FakeAudit()
        host_grants = FakeHostGrants(grants={
            "db1.tailxxxx.ts.net": HostGrant(
                managed_by="cloud", identity="auditor", capabilities=frozenset({"file_read"})
            )
        })
        use_case = TailnetSshUseCase(
            directory=directory, executor=executor, audit=audit, host_grants=host_grants
        )

        with pytest.raises(SshCapabilityDeniedError):
            use_case.execute(TailnetSshRequest(host="db1", command="uptime", timeout_s=5))

        assert audit.denied_calls == [{
            "host": "db1.tailxxxx.ts.net", "capability": "exec",
            "identity": "auditor", "reason": "capability_denied",
        }]
        assert audit.calls == []  # no executed-call entry for a denial

    def test_local_host_with_no_host_grants_injected_is_unaffected(self) -> None:
        """The default (host_grants=None) — today's behaviour byte-for-byte."""
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        use_case = TailnetSshUseCase(directory=directory, executor=executor, audit=FakeAudit())

        use_case.execute(TailnetSshRequest(host="db1", command="uptime", timeout_s=5))

        assert executor.calls[0]["user"] is None

    def test_local_host_present_in_the_store_has_no_ceiling(self) -> None:
        """A host present on the allow-list but NOT cloud-managed keeps
        today's semantics: unrestricted, no forced identity."""
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        host_grants = FakeHostGrants(grants={
            "db1.tailxxxx.ts.net": HostGrant(managed_by=None, identity=None, capabilities=None)
        })
        use_case = TailnetSshUseCase(
            directory=directory, executor=executor, audit=FakeAudit(), host_grants=host_grants
        )

        use_case.execute(TailnetSshRequest(host="db1", command="uptime", timeout_s=5))

        assert executor.calls[0]["user"] is None

    def test_unknown_host_in_the_store_has_no_ceiling(self) -> None:
        directory = FakeDirectory()
        executor = FakeExecutor(result=_ok_result())
        host_grants = FakeHostGrants(grants={})
        use_case = TailnetSshUseCase(
            directory=directory, executor=executor, audit=FakeAudit(), host_grants=host_grants
        )

        use_case.execute(TailnetSshRequest(host="db1", command="uptime", timeout_s=5))

        assert executor.calls[0]["user"] is None
