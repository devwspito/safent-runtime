"""Tests TerminalSurfaceAdapter (FR-027/028)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import (
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.terminal_surface_adapter import (
    TerminalSurfaceAdapter,
    hash_canonical_action,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class TestCapture:
    async def test_capture_echo(self) -> None:
        adapter = TerminalSurfaceAdapter()
        action = await adapter.capture(
            intent_desc="echo OK",
            params={"argv": ["echo", "hello"], "cwd": "/tmp"},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        assert action.surface_kind == SurfaceKind.TERMINAL
        assert action.payload["argv"] == ["echo", "hello"]
        assert action.payload["exit_code"] == 0
        assert "hello" in action.payload["stdout_redacted"]

    async def test_capture_rejects_denylisted_command(self) -> None:
        adapter = TerminalSurfaceAdapter()
        with pytest.raises(ValueError, match="denylist"):
            await adapter.capture(
                intent_desc="bad",
                params={"argv": ["rm", "-rf", "/"], "cwd": "/tmp"},
                tenant_id=uuid4(),
                human_operator_id=uuid4(),
            )

    async def test_capture_requires_argv(self) -> None:
        adapter = TerminalSurfaceAdapter()
        with pytest.raises(ValueError, match="argv"):
            await adapter.capture(
                intent_desc="empty",
                params={"argv": [], "cwd": "/tmp"},
                tenant_id=uuid4(),
                human_operator_id=uuid4(),
            )


class TestReplay:
    async def test_replay_echo_ok(self) -> None:
        adapter = TerminalSurfaceAdapter()
        captured = await adapter.capture(
            intent_desc="say hi",
            params={"argv": ["echo", "hi"], "cwd": "/tmp"},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        outcome = await adapter.replay(captured)
        assert outcome.status == ReplayStatus.EXECUTED_OK
        assert "hi" in outcome.result.get("stdout", "")

    async def test_replay_rejects_surface_mismatch(self) -> None:
        adapter = TerminalSurfaceAdapter()
        from hermes.agents_os.domain.ports.surface_adapter_port import (
            CapturedAction,
        )

        bad = CapturedAction(
            surface_kind=SurfaceKind.BROWSER,
            intent_desc="wrong surface",
            payload={"argv": ["echo", "x"]},
        )
        outcome = await adapter.replay(bad)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY
        assert outcome.error is not None
        assert "TERMINAL" in outcome.error

    async def test_replay_nonzero_exit_fails(self) -> None:
        adapter = TerminalSurfaceAdapter()
        captured = await adapter.capture(
            intent_desc="false",
            params={"argv": ["false"], "cwd": "/tmp"},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        outcome = await adapter.replay(captured)
        assert outcome.status == ReplayStatus.EXECUTED_FAILED


class TestSigningSerialization:
    async def test_serialize_deterministic(self) -> None:
        adapter = TerminalSurfaceAdapter()
        captured1 = await adapter.capture(
            intent_desc="hello",
            params={"argv": ["echo", "x"], "cwd": "/tmp"},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        captured2 = await adapter.capture(
            intent_desc="hello",
            params={"argv": ["echo", "x"], "cwd": "/tmp"},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        # IDs distintos, mismo contenido firmable.
        assert adapter.serialize_for_signing(captured1) == adapter.serialize_for_signing(
            captured2
        )
        assert hash_canonical_action(adapter, captured1) == hash_canonical_action(
            adapter, captured2
        )

    async def test_serialize_excludes_stdout(self) -> None:
        """stdout/stderr NO se firman porque pueden variar entre runs.

        argv SÍ se firma porque es parte de la intención de la skill.
        """
        adapter = TerminalSurfaceAdapter()
        captured = await adapter.capture(
            intent_desc="hello world script",
            # cat /etc/hostname produce un hostname dinámico en stdout
            params={"argv": ["cat", "/etc/hostname"], "cwd": "/tmp"},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        sig = adapter.serialize_for_signing(captured)
        # El hostname está en stdout_redacted pero NO en la firma
        hostname_in_stdout = captured.payload["stdout_redacted"].strip()
        assert hostname_in_stdout  # debe haber hostname
        assert hostname_in_stdout.encode() not in sig, (
            "stdout no debe formar parte de la firma — varía entre runs"
        )
