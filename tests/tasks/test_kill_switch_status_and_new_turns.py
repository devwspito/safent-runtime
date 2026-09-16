"""025 Top-KILL — kill-switch REST surface: status persistence + new-turn refusal.

Covers:
  - AgentStatePort.status() state machine (engaged/reason/changed_by/changed_at)
    for both InMemoryAgentState and SqliteAgentState.
  - SqliteAgentState.status() SURVIVES a restart — a NEW instance pointed at the
    SAME db_path sees the persisted engaged state (file under the daemon's
    state dir, per the task contract).
  - ControlPlaneService.enqueue() refuses a NEW chat turn with a TYPED error
    (EnqueueBlockedByKillSwitch) while engaged — before touching the queue —
    and admits it again once released. Mirrors the existing tool-dispatch gate
    (capability_broker Paso 0 / tests/tasks/test_kill_switch.py) but for the
    "new chat turn" surface, which had no such gate before this fix.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hermes.tasks.control_plane.application.control_plane_service import (
    ControlPlaneService,
)
from hermes.tasks.control_plane.domain.ports import (
    AuthenticatedChannel,
    EnqueueBlockedByKillSwitch,
)
from hermes.tasks.infrastructure.sqlite_agent_state import SqliteAgentState
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000
_OPERATOR = UUID(int=_OPERATOR_UID)
_TENANT_ID = uuid4()
_AUTHORIZED_UIDS: frozenset[int] = frozenset({_OPERATOR_UID})


def _channel() -> AuthenticatedChannel:
    return AuthenticatedChannel(sender_uid=_OPERATOR_UID)


def _audit_deps(tmp_path: Path):
    """Real signer+audit_repo for SqliteAgentState — mandatory since CLI-N4
    (specs/025-safent-repaso matriz-final-39eeb8e): these tests only care
    about status() persistence, not the audit chain content, but the state
    object now refuses to construct without them."""
    import os

    from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
    from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository

    signer = AuditHashChainSigner(signing_key=os.urandom(32))
    audit_repo = SqliteAuditRepository(db_path=tmp_path / "audit.db")
    return signer, audit_repo


def _make_service(state) -> ControlPlaneService:
    return ControlPlaneService(
        queue=InMemoryWorkQueue(),
        agent_state=state,
        authorized_uids=_AUTHORIZED_UIDS,
        tenant_id=_TENANT_ID,
    )


# ---------------------------------------------------------------------------
# status() state machine
# ---------------------------------------------------------------------------


class TestInMemoryAgentStateStatus:
    @pytest.mark.asyncio
    async def test_fresh_state_is_not_engaged(self) -> None:
        state = InMemoryAgentState()
        status = await state.status()
        assert status == {"engaged": False, "reason": None, "changed_by": None, "changed_at": None}

    @pytest.mark.asyncio
    async def test_pause_engages_with_reason_and_actor(self) -> None:
        state = InMemoryAgentState()
        await state.pause(by=_OPERATOR, reason="cosa rara en los logs")
        status = await state.status()
        assert status["engaged"] is True
        assert status["reason"] == "cosa rara en los logs"
        assert status["changed_by"] == str(_OPERATOR)
        assert status["changed_at"] is not None

    @pytest.mark.asyncio
    async def test_resume_disengages_and_clears_reason(self) -> None:
        state = InMemoryAgentState()
        await state.pause(by=_OPERATOR, reason="freno")
        await state.resume(by=_OPERATOR)
        status = await state.status()
        assert status["engaged"] is False
        assert status["reason"] is None


class TestSqliteAgentStateStatusPersistsAcrossRestart:
    @pytest.mark.asyncio
    async def test_engaged_state_survives_new_instance_same_db_path(self, tmp_path: Path) -> None:
        db_path = tmp_path / "shell-state.db"
        signer, audit_repo = _audit_deps(tmp_path)
        first = SqliteAgentState(db_path=db_path, signer=signer, audit_repo=audit_repo)
        await first.pause(by=_OPERATOR, reason="restart test")

        # A restart re-constructs the state object pointed at the SAME file —
        # nothing carries over in-process (no singleton), only the file does.
        second = SqliteAgentState(db_path=db_path, signer=signer, audit_repo=audit_repo)
        status = await second.status()

        assert status["engaged"] is True
        assert status["reason"] == "restart test"
        assert status["changed_by"] == str(_OPERATOR)

    @pytest.mark.asyncio
    async def test_released_state_also_survives_restart(self, tmp_path: Path) -> None:
        db_path = tmp_path / "shell-state.db"
        signer, audit_repo = _audit_deps(tmp_path)
        first = SqliteAgentState(db_path=db_path, signer=signer, audit_repo=audit_repo)
        await first.pause(by=_OPERATOR, reason="temporary")
        await first.resume(by=_OPERATOR)

        second = SqliteAgentState(db_path=db_path, signer=signer, audit_repo=audit_repo)
        status = await second.status()

        assert status["engaged"] is False
        assert status["reason"] is None


# ---------------------------------------------------------------------------
# New chat turn refused with a typed error while engaged
# ---------------------------------------------------------------------------


class TestNewChatTurnRefusedWhileEngaged:
    @pytest.mark.asyncio
    async def test_enqueue_blocked_while_engaged(self) -> None:
        state = InMemoryAgentState()
        await state.pause(by=_OPERATOR, reason="freno de emergencia")
        service = _make_service(state)

        with pytest.raises(EnqueueBlockedByKillSwitch):
            await service.enqueue(
                channel=_channel(), trigger_kind="chat_message", text="hola",
            )

    @pytest.mark.asyncio
    async def test_queue_untouched_when_blocked(self) -> None:
        state = InMemoryAgentState()
        queue = InMemoryWorkQueue()
        service = ControlPlaneService(
            queue=queue, agent_state=state,
            authorized_uids=_AUTHORIZED_UIDS, tenant_id=_TENANT_ID,
        )
        await state.pause(by=_OPERATOR, reason="freno")

        with pytest.raises(EnqueueBlockedByKillSwitch):
            await service.enqueue(channel=_channel(), trigger_kind="chat_message", text="hola")

        assert len(queue._items) == 0

    @pytest.mark.asyncio
    async def test_enqueue_admitted_again_after_release(self) -> None:
        state = InMemoryAgentState()
        service = _make_service(state)
        await state.pause(by=_OPERATOR, reason="freno")
        await state.resume(by=_OPERATOR)

        result = await service.enqueue(
            channel=_channel(), trigger_kind="chat_message", text="hola de nuevo",
        )

        assert result.task_id is not None

    @pytest.mark.asyncio
    async def test_enqueue_not_blocked_when_never_engaged(self) -> None:
        """No regression: the common (never-engaged) path is untouched."""
        state = InMemoryAgentState()
        service = _make_service(state)

        result = await service.enqueue(
            channel=_channel(), trigger_kind="chat_message", text="hola",
        )

        assert result.task_id is not None
