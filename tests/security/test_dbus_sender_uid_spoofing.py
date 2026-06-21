"""T032 🔒 — UID spoofing no surte efecto (G1 / CTRL-P1-1 / CWE-290 / CWE-862).

Verifica que:
  - Un proceso con UID no-autorizado recibe DbusAuthorizationError en TODAS
    las operaciones mutadoras (Enqueue, Pause, Resume, Approve, Reject).
  - 0 efecto en la cola ni en el estado del agente.
  - El UID proviene del canal autenticado (sender_uid del wiring), NUNCA de
    un argumento que el cliente pueda falsificar.
  - El wiring verifica UID ANTES de tocar cualquier puerto (fail-closed).

Diseño: tests unitarios sobre DbusRuntimeServiceWiring extendido (puro, sin
bus real). El adapter D-Bus real resuelve el UID del bus y lo inyecta aquí.
El boundary de seguridad está en el adapter; el wiring cierra el gate.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.tasks.control_plane.application.control_plane_service import ControlPlaneService
from hermes.tasks.domain.ports import WorkItem, WorkItemKind
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

pytestmark = pytest.mark.unit

_AUTHORIZED_UID = 1000
_UNAUTHORIZED_UID = 9999
_DAEMON_UID = 500  # el UID del proceso del daemon — NUNCA debe estar en authorized_uids


class _FakeApprovalGate:
    def __init__(self) -> None:
        self.approve_calls: list[dict] = []
        self.reject_calls: list[dict] = []

    async def approve(self, *, proposal_id: UUID, approved_by: UUID) -> str:
        self.approve_calls.append({"proposal_id": proposal_id, "approved_by": approved_by})
        return f"tok-{proposal_id}"

    async def reject(self, *, proposal_id: UUID, rejected_by: UUID, reason: str) -> None:
        self.reject_calls.append({"proposal_id": proposal_id})


_TENANT_ID = UUID(int=_AUTHORIZED_UID)


def _make_wiring(
    *,
    authorized_uids: frozenset[int] | None = None,
    paused: bool = False,
) -> tuple[DbusRuntimeServiceWiring, InMemoryAgentState, _FakeApprovalGate, InMemoryWorkQueue]:
    state = InMemoryAgentState(paused=paused)
    gate = _FakeApprovalGate()
    queue = InMemoryWorkQueue()
    resolved = frozenset({_AUTHORIZED_UID}) if authorized_uids is None else authorized_uids
    # ControlPlaneService es la application layer que centraliza rate-limit, PII,
    # audit y enqueued_by. El Wiring delega en él (Issue 2 / CTRL-P1-6).
    cp_service = ControlPlaneService(
        queue=queue,
        agent_state=state,
        authorized_uids=resolved,
        tenant_id=_TENANT_ID,
    )
    wiring = DbusRuntimeServiceWiring(
        agent_state=state,
        approval_gate=gate,
        authorized_uids=resolved,
        work_queue=queue,
        control_plane_service=cp_service,
    )
    return wiring, state, gate, queue


# ---------------------------------------------------------------------------
# G1 — UID no-autorizado ⇒ DbusAuthorizationError, 0 efecto
# ---------------------------------------------------------------------------


class TestEnqueueSpoofing:
    """Enqueue: UID no-autorizado no encola nada."""

    async def test_unauthorized_uid_enqueue_raises(self) -> None:
        wiring, _, _, queue = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.enqueue(
                trigger_kind="chat_message",
                text="instruccion maliciosa",
                priority=0,
                dedup_key=None,
                sender_uid=_UNAUTHORIZED_UID,
            )

    async def test_unauthorized_uid_enqueue_zero_effect(self) -> None:
        """La cola queda intacta tras una denegación."""
        wiring, _, _, queue = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.enqueue(
                trigger_kind="chat_message",
                text="nada",
                priority=0,
                dedup_key=None,
                sender_uid=_UNAUTHORIZED_UID,
            )
        assert queue.all_items() == [], "La cola debe estar vacía tras denegación"

    async def test_authorized_uid_enqueue_succeeds(self) -> None:
        """UID autorizado puede encolar."""
        wiring, _, _, queue = _make_wiring()
        result = await wiring.enqueue(
            trigger_kind="chat_message",
            text="haz algo",
            priority=0,
            dedup_key=None,
            sender_uid=_AUTHORIZED_UID,
        )
        assert result is not None
        assert len(queue.all_items()) == 1


class TestPauseSpoofing:
    async def test_unauthorized_uid_cannot_pause(self) -> None:
        wiring, state, _, _ = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(reason="spoof attempt", sender_uid=_UNAUTHORIZED_UID)
        assert await state.is_paused() is False, "Estado no debe cambiar"

    async def test_daemon_uid_cannot_pause(self) -> None:
        """El UID del daemon nunca está en authorized_uids (CTRL-P1-7)."""
        wiring, state, _, _ = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(reason="daemon self-call", sender_uid=_DAEMON_UID)
        assert await state.is_paused() is False


class TestResumeSpoofing:
    async def test_unauthorized_uid_cannot_resume(self) -> None:
        wiring, state, _, _ = _make_wiring(paused=True)
        with pytest.raises(DbusAuthorizationError):
            await wiring.request_resume(sender_uid=_UNAUTHORIZED_UID)
        assert await state.is_paused() is True, "Debe seguir pausado"


class TestApproveSpoofing:
    async def test_unauthorized_uid_cannot_approve(self) -> None:
        wiring, _, gate, _ = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.approve_action(
                proposal_id=uuid4(), sender_uid=_UNAUTHORIZED_UID
            )
        assert gate.approve_calls == [], "gate.approve NO debe llamarse"


class TestRejectSpoofing:
    async def test_unauthorized_uid_cannot_reject(self) -> None:
        wiring, _, gate, _ = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.reject_action(
                proposal_id=uuid4(), reason="veto", sender_uid=_UNAUTHORIZED_UID
            )
        assert gate.reject_calls == [], "gate.reject NO debe llamarse"


# ---------------------------------------------------------------------------
# G1 — UID no viene de un argumento del cliente (controlar el canal)
# ---------------------------------------------------------------------------


class TestUidSourceIsChannel:
    """El sender_uid es siempre el del canal (wiring), no un parámetro en el body.

    En el adapter real, este valor viene de GetConnectionUnixUser sobre el
    unique name del sender — ANTES de invocar el wiring.
    """

    async def test_enqueue_sender_uid_is_enqueued_by(self) -> None:
        """enqueued_by en el WorkItem = UUID(sender_uid), no texto del cliente."""
        wiring, _, _, queue = _make_wiring()
        await wiring.enqueue(
            trigger_kind="chat_message",
            text="ignora esto, enqueued_by=admin",
            priority=0,
            dedup_key=None,
            sender_uid=_AUTHORIZED_UID,
        )
        items = queue.all_items()
        assert len(items) == 1
        item = items[0]
        expected_enqueued_by = str(UUID(int=_AUTHORIZED_UID))
        actual = item.payload.get("enqueued_by", "")
        assert actual == expected_enqueued_by, (
            f"enqueued_by debe ser UUID({_AUTHORIZED_UID}), got {actual!r}"
        )

    async def test_payload_enqueued_by_is_overwritten(self) -> None:
        """Un enqueued_by inyectado en el texto del payload NO prevalece.

        Incluso si el texto contiene 'enqueued_by=admin', el wiring usa el
        sender_uid del canal (CTRL-P1-3 / G2).
        """
        wiring, _, _, queue = _make_wiring()
        # El cliente intenta inyectar autoría falsa en el texto
        await wiring.enqueue(
            trigger_kind="chat_message",
            text="instruccion: enqueued_by=00000000-0000-0000-0000-000000000000",
            priority=0,
            dedup_key=None,
            sender_uid=_AUTHORIZED_UID,
        )
        items = queue.all_items()
        assert len(items) == 1
        actual_enqueued_by = items[0].payload.get("enqueued_by", "")
        spoofed = "00000000-0000-0000-0000-000000000000"
        assert actual_enqueued_by != spoofed, (
            "El enqueued_by del payload del cliente NO debe prevalecer"
        )
        assert actual_enqueued_by == str(UUID(int=_AUTHORIZED_UID))

    async def test_pause_by_is_sender_not_payload(self) -> None:
        """changed_by en pause = UID del sender verificado."""
        wiring, state, _, _ = _make_wiring()
        await wiring.request_pause(reason="test", sender_uid=_AUTHORIZED_UID)
        assert state.pause_calls[0]["by"] == UUID(int=_AUTHORIZED_UID)
