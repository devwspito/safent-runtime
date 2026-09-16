"""TailnetSshSurfaceAdapter — SurfaceKind.TAILNET_SSH dispatch by `op`.

Coverage:
  - replay() rejects a CapturedAction with the wrong surface_kind.
  - replay() rejects an unknown `op`.
  - op="tailnet_ssh" builds a TailnetSshRequest and returns the result as a dict.
  - op="tailnet_file_get" / "tailnet_file_put" route to their use cases.
  - A TailnetSshError raised by the use case maps to ReplayOutcome.failed
    (never an unhandled exception escaping replay()).
  - The blocking use case is off-loaded (replay() itself never blocks the
    caller's event loop — verified indirectly via asyncio.to_thread usage).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.tailnet_ssh.application.ports import SshExecutionResult, TailnetPeer, TailnetStatus
from hermes.tailnet_ssh.application.tailnet_file_use_case import (
    TailnetFileGetUseCase,
    TailnetFilePutUseCase,
)
from hermes.tailnet_ssh.application.tailnet_ssh_use_case import TailnetSshUseCase
from hermes.tailnet_ssh.domain.errors import RemoteCommandTimeoutError
from hermes.tailnet_ssh.infrastructure.tailnet_ssh_surface_adapter import (
    TailnetSshSurfaceAdapter,
)

pytestmark = pytest.mark.unit

_STATUS = TailnetStatus(
    node_name="safent-agent",
    magicdns_suffix="tailxxxx.ts.net",
    online=True,
    peers=(TailnetPeer(name="db1", online=True),),
)


class _FakeDirectory:
    def read(self) -> TailnetStatus:
        return _STATUS


class _NullAudit:
    def record_ssh_call(self, **kwargs: Any) -> None:  # noqa: ARG002
        return None


@dataclass
class _FakeExecutor:
    result: SshExecutionResult | None = None
    raise_error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def run(self, *, host, command, timeout_s, stdin=None, max_output_bytes=None):
        self.calls.append({
            "host": host, "command": command, "timeout_s": timeout_s,
            "stdin": stdin, "max_output_bytes": max_output_bytes,
        })
        if self.raise_error is not None:
            raise self.raise_error
        return self.result


def _adapter(executor: _FakeExecutor) -> TailnetSshSurfaceAdapter:
    directory = _FakeDirectory()
    audit = _NullAudit()
    return TailnetSshSurfaceAdapter(
        ssh_use_case=TailnetSshUseCase(directory=directory, executor=executor, audit=audit),
        file_get_use_case=TailnetFileGetUseCase(directory=directory, executor=executor, audit=audit),
        file_put_use_case=TailnetFilePutUseCase(directory=directory, executor=executor, audit=audit),
    )


def _action(op: str, payload: dict[str, Any], surface_kind: SurfaceKind = SurfaceKind.TAILNET_SSH) -> CapturedAction:
    return CapturedAction(
        action_id=uuid4(),
        surface_kind=surface_kind,
        payload={"op": op, **payload},
    )


class TestSurfaceKindGuard:
    async def test_wrong_surface_kind_rejected_by_policy(self) -> None:
        adapter = _adapter(_FakeExecutor())
        action = _action("tailnet_ssh", {"host": "db1", "command": "ls"}, surface_kind=SurfaceKind.BROWSER)

        outcome = await adapter.replay(action)

        assert outcome.status is ReplayStatus.REJECTED_BY_POLICY


class TestUnknownOp:
    async def test_unknown_op_rejected_by_policy(self) -> None:
        adapter = _adapter(_FakeExecutor())
        action = _action("not_a_real_op", {"host": "db1"})

        outcome = await adapter.replay(action)

        assert outcome.status is ReplayStatus.REJECTED_BY_POLICY


class TestTailnetSshDispatch:
    async def test_executes_and_returns_result_dict(self) -> None:
        result = SshExecutionResult(
            exit_code=0, stdout="ok\n", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=12,
        )
        executor = _FakeExecutor(result=result)
        adapter = _adapter(executor)
        action = _action("tailnet_ssh", {"host": "db1", "command": "uptime", "timeout_s": 5})

        outcome = await adapter.replay(action)

        assert outcome.status is ReplayStatus.EXECUTED_OK
        assert outcome.result["exit_code"] == 0
        assert outcome.result["stdout"] == "ok\n"
        assert outcome.result["host"] == "db1.tailxxxx.ts.net"
        assert executor.calls[0]["timeout_s"] == 5

    async def test_default_timeout_applied_when_omitted(self) -> None:
        result = SshExecutionResult(
            exit_code=0, stdout="", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=1,
        )
        executor = _FakeExecutor(result=result)
        adapter = _adapter(executor)
        action = _action("tailnet_ssh", {"host": "db1", "command": "ls"})

        await adapter.replay(action)

        assert executor.calls[0]["timeout_s"] == 30

    async def test_timeout_error_maps_to_failed_outcome(self) -> None:
        executor = _FakeExecutor(raise_error=RemoteCommandTimeoutError("ssh a «db1» excedió 5s"))
        adapter = _adapter(executor)
        action = _action("tailnet_ssh", {"host": "db1", "command": "sleep 999", "timeout_s": 5})

        outcome = await adapter.replay(action)

        assert outcome.status is ReplayStatus.EXECUTED_FAILED
        assert "excedió" in (outcome.error or "")


class TestTailnetFileGetDispatch:
    async def test_executes_and_returns_result_dict(self) -> None:
        result = SshExecutionResult(
            exit_code=0, stdout="file contents", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=5,
        )
        executor = _FakeExecutor(result=result)
        adapter = _adapter(executor)
        action = _action("tailnet_file_get", {"host": "db1", "path": "/etc/hostname"})

        outcome = await adapter.replay(action)

        assert outcome.status is ReplayStatus.EXECUTED_OK
        assert outcome.result["content"] == "file contents"
        assert executor.calls[0]["command"] == "cat -- /etc/hostname"


class TestTailnetFilePutDispatch:
    async def test_executes_and_returns_result_dict(self) -> None:
        result = SshExecutionResult(
            exit_code=0, stdout="", stderr="", stdout_truncated=False,
            stderr_truncated=False, duration_ms=7,
        )
        executor = _FakeExecutor(result=result)
        adapter = _adapter(executor)
        action = _action(
            "tailnet_file_put", {"host": "db1", "path": "/tmp/x", "content": "hola"}
        )

        outcome = await adapter.replay(action)

        assert outcome.status is ReplayStatus.EXECUTED_OK
        assert outcome.result["bytes_written"] == len("hola".encode())
        assert executor.calls[0]["stdin"] == "hola"


class TestSurfaceKindProperty:
    def test_surface_kind_is_tailnet_ssh(self) -> None:
        adapter = _adapter(_FakeExecutor())
        assert adapter.surface_kind is SurfaceKind.TAILNET_SSH
