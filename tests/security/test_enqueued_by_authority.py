"""T033 🔒 — Autoría de encolado (G2 / SC-008 / CTRL-P1-3).

Verifica que, independientemente de lo que el cliente incluya en el payload,
el `enqueued_by` registrado en el WorkItem es SIEMPRE el UUID derivado del
sender_uid verificado por el canal autenticado.

Escenarios:
  - payload con 'enqueued_by' explícito → ignorado, prevalece sender_uid.
  - payload vacío → enqueued_by = UUID(sender_uid).
  - payload con 'enqueued_by' = 'admin' → ignorado, prevalece sender_uid.
  - 100% de intentos de spoofing-by-content registran enqueued_by=UUID(sender_uid).

Diseño: tests unitarios sobre ControlPlaneService (application layer) con
InMemoryWorkQueue + InMemoryAgentState. Sin bus real.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from hermes.tasks.control_plane.application.control_plane_service import (
    ControlPlaneService,
)
from hermes.tasks.control_plane.domain.ports import AuthenticatedChannel
from hermes.tasks.domain.ports import TaskStatus
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000
_TENANT_ID = uuid4()
_AUTHORIZED_UIDS: frozenset[int] = frozenset({_OPERATOR_UID})


def _channel(uid: int = _OPERATOR_UID) -> AuthenticatedChannel:
    return AuthenticatedChannel(sender_uid=uid)


class _FakeWakeSignal:
    def __init__(self) -> None:
        self.wakes = 0

    def wake_one(self) -> None:
        self.wakes += 1

    async def wait_for_work(self, *, timeout: float) -> bool:
        return False


def _make_service() -> tuple[ControlPlaneService, InMemoryWorkQueue, _FakeWakeSignal]:
    queue = InMemoryWorkQueue()
    state = InMemoryAgentState()
    wake = _FakeWakeSignal()
    service = ControlPlaneService(
        queue=queue,
        agent_state=state,
        authorized_uids=_AUTHORIZED_UIDS,
        tenant_id=_TENANT_ID,
        wake_signal=wake,
    )
    return service, queue, wake


# ---------------------------------------------------------------------------
# SC-008: enqueued_by = UUID(sender_uid), NUNCA del payload
# ---------------------------------------------------------------------------


class TestEnqueuedByAuthority:
    async def test_enqueued_by_is_sender_uid(self) -> None:
        """enqueued_by = UUID(sender_uid) verificado — no del contenido."""
        svc, queue, _ = _make_service()
        channel = _channel(_OPERATOR_UID)
        await svc.enqueue(
            channel=channel,
            trigger_kind="chat_message",
            text="hola agente",
            priority=0,
        )
        items = queue.all_items()
        assert len(items) == 1
        expected = str(UUID(int=_OPERATOR_UID))
        assert items[0].payload["enqueued_by"] == expected

    async def test_payload_with_enqueued_by_admin_is_ignored(self) -> None:
        """payload con enqueued_by='admin' → se descarta, prevalece sender_uid.

        CTRL-P1-3: enqueued_by del payload del cliente se SOBRESCRIBE.
        """
        svc, queue, _ = _make_service()
        channel = _channel(_OPERATOR_UID)
        # El texto podría contener metadata maliciosa — se ignora
        await svc.enqueue(
            channel=channel,
            trigger_kind="chat_message",
            text="enqueued_by=admin; rm -rf /",
            priority=0,
        )
        items = queue.all_items()
        assert len(items) == 1
        actual = items[0].payload.get("enqueued_by", "")
        assert actual != "admin"
        assert actual == str(UUID(int=_OPERATOR_UID))

    async def test_100_percent_spoofing_attempts_use_sender_uid(self) -> None:
        """100% de intentos de spoofing-by-content registran enqueued_by=UUID(sender_uid)."""
        svc, queue, _ = _make_service()
        channel = _channel(_OPERATOR_UID)
        spoofed_ids = [
            "00000000-0000-0000-0000-000000000000",
            "admin",
            str(uuid4()),
            "root",
            "ffffffff-ffff-ffff-ffff-ffffffffffff",
        ]
        for attempt in spoofed_ids:
            await svc.enqueue(
                channel=channel,
                trigger_kind="chat_message",
                text=f"enqueued_by={attempt}",
                priority=0,
                dedup_key=f"key-{attempt}",
            )

        items = queue.all_items()
        expected = str(UUID(int=_OPERATOR_UID))
        for item in items:
            actual = item.payload.get("enqueued_by", "")
            assert actual == expected, (
                f"Expected enqueued_by={expected!r}, got {actual!r} "
                f"for item {item.id}"
            )

    async def test_enqueued_by_not_overridable_via_dedup_key(self) -> None:
        """El dedup_key no afecta la autoría."""
        svc, queue, _ = _make_service()
        channel = _channel(_OPERATOR_UID)
        await svc.enqueue(
            channel=channel,
            trigger_kind="chat_message",
            text="mensaje",
            priority=0,
            dedup_key="unique-key-123",
        )
        items = queue.all_items()
        assert items[0].payload["enqueued_by"] == str(UUID(int=_OPERATOR_UID))


# ---------------------------------------------------------------------------
# Fail-closed: UID no autorizado ⇒ EnqueueNotAuthorized ANTES de tocar la cola
# ---------------------------------------------------------------------------


class TestEnqueueFailClosed:
    async def test_unauthorized_uid_raises_before_queue_touch(self) -> None:
        """UID no autorizado → EnqueueNotAuthorized, cola intacta (CTRL-P1-3)."""
        from hermes.tasks.control_plane.domain.ports import EnqueueNotAuthorized

        svc, queue, _ = _make_service()
        channel = _channel(uid=8888)  # UID no en authorized_uids
        with pytest.raises(EnqueueNotAuthorized):
            await svc.enqueue(
                channel=channel,
                trigger_kind="chat_message",
                text="intento",
                priority=0,
            )
        assert queue.all_items() == [], "Cola debe estar intacta"

    async def test_rate_limit_raises_on_flood(self) -> None:
        """Flood de Enqueue desde un UID autorizado ⇒ rate limit (CTRL-P1-6)."""
        from hermes.tasks.control_plane.application.control_plane_service import (
            EnqueueRateLimited,
        )

        svc, queue, _ = _make_service()
        channel = _channel(_OPERATOR_UID)
        # Exceder el bucket (burst + 1 extra)
        limit_hit = False
        for i in range(200):
            try:
                await svc.enqueue(
                    channel=channel,
                    trigger_kind="chat_message",
                    text=f"flood {i}",
                    priority=0,
                    dedup_key=f"flood-{i}",
                )
            except EnqueueRateLimited:
                limit_hit = True
                break
        assert limit_hit, "Rate limit debe activarse antes de 200 items"

    async def test_queue_depth_cap_blocks_enqueue(self) -> None:
        """Cola llena ⇒ EnqueueQueueFull antes de insertar (CTRL-P1-6 / CWE-770).

        Verifica que _QUEUE_DEPTH_CAP es real: al alcanzar el cap el servicio
        rechaza nuevos items antes de consumir un token del bucket.

        Se simulan _QUEUE_DEPTH_CAP items pendientes parcheando `all_items`
        para devolver WorkItems reales con status=PENDING.
        """
        from hermes.tasks.control_plane.application.control_plane_service import (
            EnqueueQueueFull,
            _QUEUE_DEPTH_CAP,
        )
        from hermes.tasks.domain.ports import WorkItem, WorkItemKind

        svc, queue, _ = _make_service()
        channel = _channel(_OPERATOR_UID)

        # Construir items PENDING suficientes para saturar el cap.
        # Usamos _QUEUE_DEPTH_CAP items para que len(pending) >= cap.
        cap_items = [
            WorkItem.new(
                tenant_id=_TENANT_ID,
                trigger_kind="chat_message",
                kind=WorkItemKind.CHAT_MESSAGE,
                payload={"enqueued_by": str(UUID(int=_OPERATOR_UID))},
                dedup_key=f"cap-test-{i}",
            )
            for i in range(_QUEUE_DEPTH_CAP)
        ]
        # Insertar directamente en el dict interno del InMemoryWorkQueue
        # sin pasar por enqueue (evitamos activar el cap prematuramente).
        for item in cap_items:
            queue._items[item.id] = item  # noqa: SLF001

        # Ahora el servicio debe rechazar el siguiente enqueue.
        with pytest.raises(EnqueueQueueFull):
            await svc.enqueue(
                channel=channel,
                trigger_kind="chat_message",
                text="intento con cola llena",
                priority=0,
            )

        # No se debe haber agregado ningún item nuevo (cola intacta en tamaño).
        assert len(queue.all_items()) == _QUEUE_DEPTH_CAP


# ---------------------------------------------------------------------------
# AuditEntry WORKITEM_ACCEPTED sincrónico
# ---------------------------------------------------------------------------


class TestAuditEntryOnEnqueue:
    async def test_audit_entry_emitted_synchronously(self) -> None:
        """AuditEntry(WORKITEM_ACCEPTED) se emite antes de devolver task_id (CTRL-P1-4)."""
        svc, queue, _ = _make_service()
        channel = _channel(_OPERATOR_UID)
        result = await svc.enqueue(
            channel=channel,
            trigger_kind="chat_message",
            text="mensaje con audit",
            priority=0,
        )
        # El servicio debe haber emitido el audit — verificamos via la cola de auditoría
        audit_entries = svc.audit_entries_emitted()
        assert len(audit_entries) >= 1
        entry = audit_entries[-1]
        from hermes.agents_os.application.audit_hash_chain import AuditKind

        assert entry.audit_kind == AuditKind.WORKITEM_ACCEPTED
        assert entry.actor == str(UUID(int=_OPERATOR_UID))


# ---------------------------------------------------------------------------
# GetQueueStatus / ListPending: metadatos solamente (CTRL-P1-5)
# ---------------------------------------------------------------------------


class TestReadOnlyMethodsPayloadRedaction:
    async def test_get_queue_status_no_payload(self) -> None:
        """GetQueueStatus devuelve solo metadatos, no payload_json (CTRL-P1-5)."""
        svc, _, _ = _make_service()
        status = await svc.get_queue_status()
        # QueueStatus es dataclass frozen con slots=True — iterar field names
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(status)}
        assert "payload_json" not in field_names
        assert "instruction" not in field_names

    async def test_list_pending_no_payload(self) -> None:
        """ListPending devuelve solo (task_id, kind, status, enqueued_at), no payload."""
        svc, queue, _ = _make_service()
        channel = _channel(_OPERATOR_UID)
        await svc.enqueue(
            channel=channel,
            trigger_kind="chat_message",
            text="secreto PII",
            priority=0,
        )
        tasks = await svc.list_pending(limit=10)
        assert len(tasks) == 1
        task_view = tasks[0]
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(task_view)}
        assert "payload_json" not in field_names
        assert "instruction" not in field_names
        assert "text" not in field_names

    async def test_get_task_status_no_payload(self) -> None:
        """GetTaskStatus devuelve solo metadatos, no instruction/payload."""
        svc, queue, _ = _make_service()
        channel = _channel(_OPERATOR_UID)
        result = await svc.enqueue(
            channel=channel,
            trigger_kind="chat_message",
            text="datos privados",
            priority=0,
        )
        status_view = await svc.get_task_status(task_id=result.task_id)
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(status_view)}
        assert "payload_json" not in field_names
        assert "instruction" not in field_names
        assert "text" not in field_names
