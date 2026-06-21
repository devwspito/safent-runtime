"""T048 🔒 — Tests authZ de DbusRuntimeServiceWiring (CTRL-12 / KILL-1 / SC-004 / CWE-862).

Cubre:
- Sender UID autorizado puede pausar / reanudar (CTRL-12/KILL-1).
- Sender UID NO autorizado => DbusAuthorizationError, no ejecuta (fail-closed).
- approved_by / changed_by = identidad verificada del sender (SC-004 / CWE-862).
- ApproveAction NO requiere parámetro de identidad del cliente — la identidad
  viene del sender_uid del bus (nunca del payload).
- RejectAction idem.
- kill-switch + HITL en el mismo wiring coexisten correctamente.
- sender_uid NO verificable (no en authorized_uids) => deniega siempre, para
  toda operación.

Estos tests son puramente unitarios: no hay D-Bus real. El DbusAdapter
real (binding al bus) se verifica en integración en personal-desktop.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
    HitlApprovalResult,
)
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_AUTHORIZED_UID = 1000
_UNAUTHORIZED_UID = 9999
_ROOT_UID = 0
_SIGNING_KEY = os.urandom(32)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeApprovalGate:
    """ApprovalGatePort fake que registra llamadas."""

    def __init__(self) -> None:
        self.approve_calls: list[dict] = []
        self.reject_calls: list[dict] = []
        self._pending: set[UUID] = set()

    def add_pending(self, proposal_id: UUID) -> None:
        self._pending.add(proposal_id)

    async def register_pending(self, *, proposal_id, **_) -> None:
        self._pending.add(proposal_id)

    async def approve(self, *, proposal_id: UUID, approved_by: UUID) -> str:
        self.approve_calls.append({"proposal_id": proposal_id, "approved_by": approved_by})
        return f"fake-token-{proposal_id}"

    async def reject(self, *, proposal_id: UUID, rejected_by: UUID, reason: str) -> None:
        self.reject_calls.append({
            "proposal_id": proposal_id,
            "rejected_by": rejected_by,
            "reason": reason,
        })

    async def verify_token(self, *, proposal_id: UUID, token: str) -> bool:
        return token == f"fake-token-{proposal_id}"

    async def approved_token_for(self, proposal_id: UUID) -> str | None:
        return None


_DEFAULT_AUTHORIZED = frozenset({_AUTHORIZED_UID})


def _make_wiring(
    *,
    authorized_uids: frozenset[int] | None = None,
    paused: bool = False,
) -> tuple[DbusRuntimeServiceWiring, InMemoryAgentState, _FakeApprovalGate]:
    state = InMemoryAgentState(paused=paused)
    gate = _FakeApprovalGate()
    resolved = _DEFAULT_AUTHORIZED if authorized_uids is None else authorized_uids
    wiring = DbusRuntimeServiceWiring(
        agent_state=state,
        approval_gate=gate,
        authorized_uids=resolved,
    )
    return wiring, state, gate


# ---------------------------------------------------------------------------
# Kill-switch authZ (CTRL-12/KILL-1)
# ---------------------------------------------------------------------------


class TestPauseAuthZ:
    async def test_authorized_uid_can_pause(self) -> None:
        """UID autorizado puede pausar el agente (CTRL-12)."""
        wiring, state, _ = _make_wiring()
        await wiring.request_pause(reason="operator kill-switch", sender_uid=_AUTHORIZED_UID)
        assert await state.is_paused() is True

    async def test_unauthorized_uid_cannot_pause(self) -> None:
        """UID NO autorizado ⇒ DbusAuthorizationError; estado NO cambia (CWE-862)."""
        wiring, state, _ = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(reason="attempt", sender_uid=_UNAUTHORIZED_UID)
        assert await state.is_paused() is False, "Estado no debe cambiar si se deniega"

    async def test_pause_changed_by_is_sender_not_payload(self) -> None:
        """changed_by = UID del sender, no un argumento del cliente (SC-004/CWE-862)."""
        wiring, state, _ = _make_wiring()
        await wiring.request_pause(reason="test", sender_uid=_AUTHORIZED_UID)
        # El InMemoryAgentState registra la llamada con el `by` real
        assert len(state.pause_calls) == 1
        call = state.pause_calls[0]
        expected_operator = UUID(int=_AUTHORIZED_UID)
        assert call["by"] == expected_operator, (
            f"changed_by debe ser el UID del sender {_AUTHORIZED_UID}. "
            f"Got: {call['by']}"
        )

    async def test_root_uid_only_authorized_if_in_set(self) -> None:
        """UID 0 (root) solo puede pausar si está en authorized_uids."""
        wiring, state, _ = _make_wiring(authorized_uids=frozenset({_ROOT_UID}))
        await wiring.request_pause(reason="root pause", sender_uid=_ROOT_UID)
        assert await state.is_paused() is True

    async def test_root_uid_denied_if_not_in_set(self) -> None:
        """UID 0 NO autorizado si no está en authorized_uids (no hay excepción root)."""
        wiring, state, _ = _make_wiring(authorized_uids=frozenset({_AUTHORIZED_UID}))
        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(reason="root attempt", sender_uid=_ROOT_UID)


class TestResumeAuthZ:
    async def test_authorized_uid_can_resume(self) -> None:
        """UID autorizado puede reanudar el agente."""
        wiring, state, _ = _make_wiring(paused=True)
        await wiring.request_resume(sender_uid=_AUTHORIZED_UID)
        assert await state.is_paused() is False

    async def test_unauthorized_uid_cannot_resume(self) -> None:
        """UID NO autorizado ⇒ DbusAuthorizationError; estado NO cambia."""
        wiring, state, _ = _make_wiring(paused=True)
        with pytest.raises(DbusAuthorizationError):
            await wiring.request_resume(sender_uid=_UNAUTHORIZED_UID)
        assert await state.is_paused() is True, "Estado debe seguir pausado si se deniega"

    async def test_resume_changed_by_is_sender(self) -> None:
        """changed_by en resume = UID del sender (SC-004)."""
        wiring, state, _ = _make_wiring(paused=True)
        await wiring.request_resume(sender_uid=_AUTHORIZED_UID)
        assert len(state.resume_calls) == 1
        assert state.resume_calls[0]["by"] == UUID(int=_AUTHORIZED_UID)


# ---------------------------------------------------------------------------
# HITL authZ (SC-004 / T043/T044 re-enrutado a D-Bus)
# ---------------------------------------------------------------------------


class TestApproveActionAuthZ:
    async def test_authorized_uid_can_approve(self) -> None:
        """UID autorizado puede aprobar una acción HIGH."""
        wiring, _, gate = _make_wiring()
        proposal_id = uuid4()
        result = await wiring.approve_action(
            proposal_id=proposal_id,
            sender_uid=_AUTHORIZED_UID,
        )
        assert isinstance(result, HitlApprovalResult)
        assert result.approval_token  # token no vacío
        assert len(gate.approve_calls) == 1

    async def test_unauthorized_uid_cannot_approve(self) -> None:
        """UID NO autorizado ⇒ DbusAuthorizationError; gate.approve NO llamado."""
        wiring, _, gate = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.approve_action(
                proposal_id=uuid4(),
                sender_uid=_UNAUTHORIZED_UID,
            )
        assert len(gate.approve_calls) == 0, "gate.approve NO debe llamarse si se deniega"

    async def test_approved_by_is_sender_not_client_payload(self) -> None:
        """approved_by = UID del sender del bus, no un argumento del cliente (SC-004)."""
        wiring, _, gate = _make_wiring()
        proposal_id = uuid4()
        result = await wiring.approve_action(
            proposal_id=proposal_id,
            sender_uid=_AUTHORIZED_UID,
        )
        expected = UUID(int=_AUTHORIZED_UID)
        assert result.approved_by == expected, (
            f"approved_by debe ser el UID del sender {_AUTHORIZED_UID}. "
            f"Got: {result.approved_by}"
        )
        # Y el gate también recibe el mismo operator_id
        assert gate.approve_calls[0]["approved_by"] == expected

    async def test_approve_does_not_trigger_run_cycle(self) -> None:
        """ApproveAction NO dispara run_cycle — solo resuelve la aprobación (NFR-001).

        El loop retoma la tarea por su cuenta en la próxima vuelta. El wiring
        no tiene referencia a ningún engine ni orquestador.
        """
        wiring, _, gate = _make_wiring()
        # El wiring solo debe tener state + gate — sin engine
        assert not hasattr(wiring, "_engine"), (
            "DbusRuntimeServiceWiring NO debe tener _engine (NFR-001)"
        )
        # Aprobar no debe lanzar ni tener efecto secundario en el loop
        await wiring.approve_action(proposal_id=uuid4(), sender_uid=_AUTHORIZED_UID)


class TestRejectActionAuthZ:
    async def test_authorized_uid_can_reject(self) -> None:
        """UID autorizado puede rechazar una acción HIGH."""
        wiring, _, gate = _make_wiring()
        proposal_id = uuid4()
        await wiring.reject_action(
            proposal_id=proposal_id,
            reason="operator veto",
            sender_uid=_AUTHORIZED_UID,
        )
        assert len(gate.reject_calls) == 1
        assert gate.reject_calls[0]["proposal_id"] == proposal_id

    async def test_unauthorized_uid_cannot_reject(self) -> None:
        """UID NO autorizado ⇒ DbusAuthorizationError; gate.reject NO llamado."""
        wiring, _, gate = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.reject_action(
                proposal_id=uuid4(),
                reason="attempt",
                sender_uid=_UNAUTHORIZED_UID,
            )
        assert len(gate.reject_calls) == 0

    async def test_rejected_by_is_sender_not_payload(self) -> None:
        """rejected_by = UID del sender del bus (SC-004 / CWE-862)."""
        wiring, _, gate = _make_wiring()
        proposal_id = uuid4()
        await wiring.reject_action(
            proposal_id=proposal_id,
            reason="manual reject",
            sender_uid=_AUTHORIZED_UID,
        )
        expected = UUID(int=_AUTHORIZED_UID)
        assert gate.reject_calls[0]["rejected_by"] == expected

    async def test_reject_does_not_trigger_run_cycle(self) -> None:
        """RejectAction NO dispara run_cycle (NFR-001)."""
        wiring, _, _ = _make_wiring()
        assert not hasattr(wiring, "_engine")


# ---------------------------------------------------------------------------
# Fail-closed: operaciones no-existentes deniegan
# ---------------------------------------------------------------------------


class TestFailClosed:
    async def test_empty_authorized_set_denies_all(self) -> None:
        """authorized_uids vacío ⇒ toda operación denegada (fail-closed)."""
        wiring, state, _ = _make_wiring(authorized_uids=frozenset())
        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(reason="test", sender_uid=_AUTHORIZED_UID)
        assert await state.is_paused() is False

    async def test_multiple_authorized_uids(self) -> None:
        """Varios UIDs autorizados — cualquiera puede pausar."""
        wiring, state, _ = _make_wiring(
            authorized_uids=frozenset({_AUTHORIZED_UID, 1001, 1002})
        )
        await wiring.request_pause(reason="uid-1001", sender_uid=1001)
        assert await state.is_paused() is True


# ---------------------------------------------------------------------------
# Issue 2: Wiring.enqueue delega en ControlPlaneService (CTRL-P1-6 / CWE-770)
# ---------------------------------------------------------------------------


class TestWiringEnqueueDelegation:
    """Verifica que la ruta de producción (D-Bus → Wiring) aplica rate-limit.

    CTRL-P1-6 / CWE-770: el rate-limit vivía SOLO en ControlPlaneService,
    que era código muerto en producción. El Wiring ahora delega en el service.
    """

    def _make_wiring_with_cp(self) -> tuple[DbusRuntimeServiceWiring, object]:
        from uuid import uuid4

        from hermes.tasks.control_plane.application.control_plane_service import (
            ControlPlaneService,
        )
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

        state = InMemoryAgentState()
        gate = _FakeApprovalGate()
        queue = InMemoryWorkQueue()
        tenant_id = uuid4()

        cp_service = ControlPlaneService(
            queue=queue,
            agent_state=state,
            authorized_uids=frozenset({_AUTHORIZED_UID}),
            tenant_id=tenant_id,
        )
        wiring = DbusRuntimeServiceWiring(
            agent_state=state,
            approval_gate=gate,
            authorized_uids=frozenset({_AUTHORIZED_UID}),
            work_queue=queue,
            control_plane_service=cp_service,
        )
        return wiring, queue

    async def test_enqueue_via_wiring_applies_rate_limit(self) -> None:
        """Flood por D-Bus ⇒ EnqueueRateLimited desde la ruta de producción.

        Antes del fix, el rate-limit (CTRL-P1-6) era código muerto en producción
        porque el Wiring tenía su propia implementación de enqueue sin rate-limit.
        """
        from hermes.tasks.control_plane.application.control_plane_service import (
            EnqueueRateLimited,
        )

        wiring, _ = self._make_wiring_with_cp()
        limit_hit = False
        for i in range(200):
            try:
                await wiring.enqueue(
                    trigger_kind="chat_message",
                    text=f"msg {i}",
                    priority=0,
                    dedup_key=f"w-{i}",
                    sender_uid=_AUTHORIZED_UID,
                )
            except EnqueueRateLimited:
                limit_hit = True
                break
        assert limit_hit, "Rate limit debe activarse en la ruta de producción (D-Bus)"

    async def test_enqueue_without_cp_raises(self) -> None:
        """Wiring sin ControlPlaneService ⇒ NotImplementedError en Enqueue."""
        wiring, _, _ = _make_wiring()
        with pytest.raises(NotImplementedError, match="control_plane_service"):
            await wiring.enqueue(
                trigger_kind="chat_message",
                text="test",
                priority=0,
                dedup_key=None,
                sender_uid=_AUTHORIZED_UID,
            )

    async def test_unauthorized_uid_blocked_before_cp(self) -> None:
        """UID no autorizado ⇒ DbusAuthorizationError ANTES de llamar al service."""
        wiring, _ = self._make_wiring_with_cp()
        with pytest.raises(DbusAuthorizationError):
            await wiring.enqueue(
                trigger_kind="chat_message",
                text="spoof",
                priority=0,
                dedup_key=None,
                sender_uid=_UNAUTHORIZED_UID,
            )
