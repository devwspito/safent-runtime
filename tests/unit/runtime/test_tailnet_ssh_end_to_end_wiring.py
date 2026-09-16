"""spec 022 v2 — end-to-end: Step 1.6-tailnet_ssh gate -> ToolSpec handler ->
CapabilityBroker.dispatch -> SurfaceAdapterDispatcher -> TailnetSshSurfaceAdapter
-> TailnetSshUseCase -> SubprocessSshExecutor -> a FAKE `ssh` on PATH.

Simulates what the Nous engine does per tool call: call the pre_tool_call
hook first; only if it returns None (allow) does the ToolSpec handler ever
run. This is the one test that walks the FULL chain in one process, proving:

  - An unapproved host is blocked by the gate BEFORE the fake `ssh` is ever
    spawned (no marker file written).
  - An ALREADY-approved host (pre-populated allow-list) passes the gate,
    and the handler's broker.dispatch call reaches the real executor, which
    spawns the fake `ssh` and returns its stdout back through the whole
    chain to the tool-call result dict.

No real ssh, no real network (per session policy) — same fake-ssh-on-PATH
technique as tests/unit/tailnet_ssh/test_ssh_subprocess_executor.py.
"""

from __future__ import annotations

import asyncio
import stat
import sys
import textwrap
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import CapturedAction, ReplayStatus
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.domain.ports import ConsentContext, ExecutionOutcome, ExecutionStatus
from hermes.capabilities.infrastructure.surface_adapter_dispatcher import SurfaceAdapterDispatcher
from hermes.runtime.capability_tool_specs import build_capability_tool_specs
from hermes.runtime.security_hook import make_pre_tool_call_hook
from hermes.tailnet_ssh.application.ports import TailnetPeer, TailnetStatus
from hermes.tailnet_ssh.application.tailnet_file_use_case import (
    TailnetFileGetUseCase,
    TailnetFilePutUseCase,
)
from hermes.tailnet_ssh.application.tailnet_ssh_use_case import TailnetSshUseCase
from hermes.tailnet_ssh.infrastructure.ssh_subprocess_executor import SubprocessSshExecutor
from hermes.tailnet_ssh.infrastructure.tailnet_ssh_surface_adapter import (
    TailnetSshSurfaceAdapter,
)

pytestmark = pytest.mark.unit

_TENANT = UUID("20000000-0000-0000-0000-000000000009")
_OPERATOR = UUID("20000000-0000-0000-0000-00000000000a")
_STATUS = TailnetStatus(
    node_name="safent-agent", magicdns_suffix="tailxxxx.ts.net", online=True,
    peers=(TailnetPeer(name="db1", online=True),),
)

_FAKE_SSH_SOURCE = textwrap.dedent("""\
    #!{python}
    import os, sys
    marker = os.environ.get("FAKE_SSH_MARKER_PATH")
    if marker:
        open(marker, "w", encoding="utf-8").write("ssh-was-invoked")
    sys.stdout.write("hello-from-remote\\n")
    sys.exit(0)
""")


def _write_fake_ssh(tmp_path: Path) -> Path:
    script = tmp_path / "ssh"
    script.write_text(_FAKE_SSH_SOURCE.format(python=sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


class _FakeDirectory:
    def read(self) -> TailnetStatus:
        return _STATUS


class _FakeAllowlist:
    def __init__(self) -> None:
        self.allowed: set[str] = set()

    def is_allowed(self, host: str) -> bool:
        return host in self.allowed

    def allow(self, host: str) -> None:
        self.allowed.add(host)


class _NullAudit:
    def record_ssh_call(self, **kwargs) -> None:  # noqa: ARG002
        return None


def _patch_gate_infra(monkeypatch, *, allowlist: _FakeAllowlist) -> None:
    import hermes.tailnet_ssh.infrastructure.json_host_allowlist_store as allowlist_mod
    import hermes.tailnet_ssh.infrastructure.status_json_directory as directory_mod

    monkeypatch.setattr(directory_mod, "StatusJsonTailnetDirectory", lambda: _FakeDirectory())
    monkeypatch.setattr(allowlist_mod, "JsonHostAllowlistStore", lambda: allowlist)


def _patch_conversation(monkeypatch, conv_id: str) -> None:
    import hermes.runtime.conversation_task_registry as reg

    monkeypatch.setattr(reg, "get_conversation_for_task", lambda _t: conv_id)
    monkeypatch.setattr(reg, "get_current_cycle_agent", lambda: "cerebro")


class _RealDispatchBroker:
    """Minimal CapabilityBrokerPort that dispatches through a REAL
    SurfaceAdapterDispatcher — proves the tool-spec handler's proposal
    actually reaches TailnetSshSurfaceAdapter, not a mock."""

    def __init__(self, dispatcher: SurfaceAdapterDispatcher) -> None:
        self._dispatcher = dispatcher

    async def dispatch(self, proposal, consent_context, **kwargs) -> ExecutionOutcome:  # noqa: ARG002
        action = CapturedAction(
            surface_kind=SurfaceKind.TAILNET_SSH, payload=proposal.parameters,
        )
        outcome = await self._dispatcher.replay(action)
        status = (
            ExecutionStatus.EXECUTED
            if outcome.status is ReplayStatus.EXECUTED_OK
            else ExecutionStatus.FAILED
        )
        return ExecutionOutcome(proposal_id=proposal.proposal_id, status=status, result=outcome.result)


def _build_dispatcher(tmp_path: Path) -> SurfaceAdapterDispatcher:
    directory = _FakeDirectory()
    executor = SubprocessSshExecutor(
        ssh_binary=str(_write_fake_ssh(tmp_path)), known_hosts_path=tmp_path / "known_hosts",
    )
    audit = _NullAudit()
    adapter = TailnetSshSurfaceAdapter(
        ssh_use_case=TailnetSshUseCase(directory=directory, executor=executor, audit=audit),
        file_get_use_case=TailnetFileGetUseCase(directory=directory, executor=executor, audit=audit),
        file_put_use_case=TailnetFilePutUseCase(directory=directory, executor=executor, audit=audit),
    )
    return SurfaceAdapterDispatcher(adapters={SurfaceKind.TAILNET_SSH: adapter})


def _make_hook(broker) -> object:
    """Build the pre_tool_call hook with a REAL, RUNNING event loop —
    `_check_kill_switch` bridges to it via `run_coroutine_threadsafe`, which
    needs a loop actually processing callbacks (a constructed-but-never-run
    loop just times out and fails closed)."""
    agent_state = MagicMock()
    agent_state.is_paused = AsyncMock(return_value=False)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    broker._os_native_dispatcher = None  # skip denylist check (Step 6)
    return make_pre_tool_call_hook(agent_state=agent_state, engine_loop=loop, broker=broker)


class TestGateBlocksBeforeExecutorRuns:
    def test_unapproved_host_never_reaches_fake_ssh(self, monkeypatch, tmp_path: Path) -> None:
        allowlist = _FakeAllowlist()  # empty — db1 never approved
        _patch_gate_infra(monkeypatch, allowlist=allowlist)
        _patch_conversation(monkeypatch, "conv-e2e-1")

        import hermes.runtime.security_hook as sh
        # Owner hasn't acted on the card yet -> gate blocks (never a raw
        # exception, never a silent allow).
        monkeypatch.setattr(sh, "_resolve_native_danger_approval", lambda *a, **kw: "pendiente de aprobación")
        monkeypatch.setattr(sh, "_compute_danger_route", lambda *a, **kw: (None, frozenset()))

        marker_path = tmp_path / "marker"
        monkeypatch.setenv("FAKE_SSH_MARKER_PATH", str(marker_path))

        broker = _RealDispatchBroker(_build_dispatcher(tmp_path))
        hook = _make_hook(broker)

        block = hook(tool_name="tailnet_ssh", args={"host": "db1", "command": "ls"}, task_id="task-e2e-1")

        assert block is not None
        assert not marker_path.exists(), "fake ssh must NEVER run for an unapproved host"


class TestGateApprovedHostReachesExecutor:
    async def test_already_approved_host_dispatch_reaches_fake_ssh(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        allowlist = _FakeAllowlist()
        allowlist.allow("db1.tailxxxx.ts.net")  # pre-approved, as if the owner already granted it
        _patch_gate_infra(monkeypatch, allowlist=allowlist)
        _patch_conversation(monkeypatch, "conv-e2e-2")

        marker_path = tmp_path / "marker"
        monkeypatch.setenv("FAKE_SSH_MARKER_PATH", str(marker_path))

        dispatcher = _build_dispatcher(tmp_path)
        broker = _RealDispatchBroker(dispatcher)
        hook = _make_hook(broker)

        allow = hook(tool_name="tailnet_ssh", args={"host": "db1", "command": "ls"}, task_id="task-e2e-2")
        assert allow is None, "gate must ALLOW an already-approved host with no card"

        specs, _ref = build_capability_tool_specs(
            broker=broker, consent_context=ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR),
        )
        spec = next(s for s in specs if s.name == "tailnet_ssh")

        result = await spec.handler({"host": "db1", "command": "ls"})

        assert marker_path.exists(), "fake ssh must run once the gate has allowed the call"
        assert result.get("stdout") == "hello-from-remote\n"
        assert result.get("exit_code") == 0
