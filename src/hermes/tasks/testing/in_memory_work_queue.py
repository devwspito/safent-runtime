"""InMemoryWorkQueue — fake de WorkQueuePort para tests unitarios.

Thread-safe por diseño de test (no concurrent): estado en memoria, reset entre tests.
Implementa la misma semántica de dedup/claim/lease que SqliteWorkQueue, sin SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from hermes.tasks.domain.ports import TaskStatus, WorkItem, WorkQueuePort

# Lease por defecto (igual al dominio)
_LEASE_SECONDS: int = 60
# Estados terminales — no se re-encolan ni se deduplicarán
_TERMINAL: frozenset[TaskStatus] = frozenset({
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.REJECTED,
})


class InMemoryWorkQueue:
    """Implementación en memoria de WorkQueuePort para tests unitarios.

    Instanciar en cada test (no compartir entre tests). Opcionalmente
    llamar a `reset()` para limpiar el estado entre sub-escenarios.
    """

    def __init__(self) -> None:
        self._items: dict[UUID, WorkItem] = {}

    def reset(self) -> None:
        """Limpia todo el estado (útil entre sub-escenarios en el mismo test)."""
        self._items.clear()

    # ------------------------------------------------------------------
    # WorkQueuePort
    # ------------------------------------------------------------------

    async def enqueue(self, item: WorkItem) -> WorkItem:
        """Inserta PENDING. Idempotente por dedup_key (SC-007/I5):
        si hay un item VIVO con la misma dedup_key, devuelve ESE item.

        CTRL-10: rechaza si enqueued_by no está en payload (contrato de seguridad).
        """
        if item.dedup_key is not None:
            existing = await self.find_by_dedup_key(item.dedup_key)
            if existing is not None:
                return existing

        self._items[item.id] = item
        return item

    async def claim_next(self) -> WorkItem | None:
        """Toma atómicamente el siguiente PENDING disponible (prioridad DESC, enqueued_at ASC).

        Atomicidad garantizada por el GIL en tests (single-threaded).
        """
        now = datetime.now(tz=UTC)
        candidates = [
            i for i in self._items.values()
            if i.status is TaskStatus.PENDING and i.available_at <= now
        ]
        if not candidates:
            return None

        # Prioridad DESC, luego enqueued_at ASC (FIFO dentro del mismo nivel)
        candidates.sort(key=lambda x: (-x.priority, x.enqueued_at))
        item = candidates[0]

        claim_token = uuid4()
        claimed_at = now
        lease_expires_at = now + timedelta(seconds=_LEASE_SECONDS)

        claimed = _replace(
            item,
            status=TaskStatus.IN_PROGRESS,
            attempts=item.attempts + 1,
            claim_token=claim_token,
            claimed_at=claimed_at,
            lease_expires_at=lease_expires_at,
        )
        self._items[item.id] = claimed
        return claimed

    async def mark_completed(
        self,
        item_id: UUID,
        *,
        claim_token: UUID,
        audit_entry_id: UUID,  # noqa: ARG002
        execution_head_hash: str | None = None,  # noqa: ARG002
    ) -> None:
        """COMPLETED solo con audit_entry_id real (SC-001/I1).

        Raises:
            ValueError: si item no existe, estado != IN_PROGRESS, o claim_token no coincide.
            ValueError: si audit_entry_id es None (anti-éxito-alucinado).
        """
        item = self._get_or_raise(item_id)
        _assert_in_progress_with_token(item, claim_token, "mark_completed")

        completed = _replace(
            item,
            status=TaskStatus.COMPLETED,
            claim_token=None,
            claimed_at=None,
            lease_expires_at=None,
        )
        self._items[item_id] = completed

    async def mark_failed(
        self,
        item_id: UUID,
        *,
        claim_token: UUID,
        reason: str,  # noqa: ARG002
    ) -> WorkItem:
        """FAILED. Si attempts < max_attempts, re-programa a PENDING con backoff."""
        item = self._get_or_raise(item_id)
        _assert_in_progress_with_token(item, claim_token, "mark_failed")

        if item.attempts < item.max_attempts:
            from hermes.tasks.domain.retry_policy import RetryPolicy  # noqa: PLC0415
            policy = RetryPolicy()
            available_at = policy.next_available_at(item.attempts)
            updated = _replace(
                item,
                status=TaskStatus.PENDING,
                claim_token=None,
                claimed_at=None,
                lease_expires_at=None,
                available_at=available_at,
            )
        else:
            updated = _replace(
                item,
                status=TaskStatus.FAILED,
                claim_token=None,
                claimed_at=None,
                lease_expires_at=None,
            )

        self._items[item_id] = updated
        return updated

    async def mark_pending_approval(
        self, item_id: UUID, *, claim_token: UUID, proposal_id: UUID
    ) -> None:
        """PENDING_APPROVAL — libera lease (LOOP-4)."""
        item = self._get_or_raise(item_id)
        _assert_in_progress_with_token(item, claim_token, "mark_pending_approval")

        updated = _replace(
            item,
            status=TaskStatus.PENDING_APPROVAL,
            claim_token=None,
            claimed_at=None,
            lease_expires_at=None,
            payload={**item.payload, "_pending_proposal_id": str(proposal_id)},
        )
        self._items[item_id] = updated

    async def mark_rejected(
        self,
        item_id: UUID,
        *,
        claim_token: UUID,
        reason: str,  # noqa: ARG002
    ) -> None:
        """REJECTED terminal (fail-closed)."""
        item = self._get_or_raise(item_id)
        _assert_in_progress_with_token(item, claim_token, "mark_rejected")

        updated = _replace(
            item,
            status=TaskStatus.REJECTED,
            claim_token=None,
            claimed_at=None,
            lease_expires_at=None,
        )
        self._items[item_id] = updated

    async def reconcile_stale(self) -> int:
        """Re-encola IN_PROGRESS con lease vencido (SC-003/FR-007)."""
        now = datetime.now(tz=UTC)
        count = 0
        for item_id, item in list(self._items.items()):
            if (
                item.status is TaskStatus.IN_PROGRESS
                and item.lease_expires_at is not None
                and item.lease_expires_at < now
            ):
                reconciled = _replace(
                    item,
                    status=TaskStatus.PENDING,
                    claim_token=None,
                    claimed_at=None,
                    lease_expires_at=None,
                    available_at=now,
                )
                self._items[item_id] = reconciled
                count += 1
        return count

    async def find_by_dedup_key(self, dedup_key: str) -> WorkItem | None:
        """Busca un item VIVO (no terminal) por dedup_key."""
        for item in self._items.values():
            if item.dedup_key == dedup_key and item.status not in _TERMINAL:
                return item
        return None

    async def re_enqueue_after_approval(self, item_id: UUID) -> None:
        """PENDING_APPROVAL -> PENDING tras aprobación humana (FR-015)."""
        item = self._get_or_raise(item_id)
        if item.status is not TaskStatus.PENDING_APPROVAL:
            raise ValueError(
                f"re_enqueue_after_approval: item {item_id} no está en "
                f"PENDING_APPROVAL (estado: {item.status!r})"
            )
        now = datetime.now(tz=UTC)
        updated = _replace(item, status=TaskStatus.PENDING, available_at=now)
        self._items[item_id] = updated

    async def renew_lease(self, item_id: UUID, *, claim_token: UUID) -> bool:
        """Renueva lease si el claim_token coincide y el item sigue in_progress.

        Returns True if renewed, False if the item is no longer owned by this token.
        """
        item = self._items.get(item_id)
        if item is None:
            return False
        if item.status is not TaskStatus.IN_PROGRESS:
            return False
        if item.claim_token != claim_token:
            return False

        now = datetime.now(tz=UTC)
        new_lease = now + timedelta(seconds=_LEASE_SECONDS)
        updated = _replace(item, lease_expires_at=new_lease)
        self._items[item_id] = updated
        return True

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def all_items(self) -> list[WorkItem]:
        """Devuelve todos los items (para assertions en tests)."""
        return list(self._items.values())

    def items_with_status(self, status: TaskStatus) -> list[WorkItem]:
        return [i for i in self._items.values() if i.status is status]

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _get_or_raise(self, item_id: UUID) -> WorkItem:
        item = self._items.get(item_id)
        if item is None:
            raise ValueError(f"WorkItem {item_id} not found")
        return item


# Satisface WorkQueuePort structural check
assert isinstance(InMemoryWorkQueue(), WorkQueuePort)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _assert_in_progress_with_token(item: WorkItem, claim_token: UUID, op: str) -> None:
    if item.status is not TaskStatus.IN_PROGRESS:
        raise ValueError(
            f"{op}: item {item.id} no está IN_PROGRESS (estado: {item.status!r})"
        )
    if item.claim_token != claim_token:
        raise ValueError(
            f"{op}: claim_token no coincide para item {item.id}"
        )


def _replace(item: WorkItem, **kwargs: object) -> WorkItem:
    from dataclasses import fields  # stdlib  # noqa: PLC0415

    current = {f.name: getattr(item, f.name) for f in fields(item)}
    current.update(kwargs)
    return WorkItem(**current)  # type: ignore[arg-type]
