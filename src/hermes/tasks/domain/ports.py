"""Puertos del bounded context `tasks` — loop autónomo + cola durable + estado.

Source of truth de las firmas. Domain layer: cero framework, cero dependencia
de `agents_os`. Las implementaciones (SQLite) viven en `tasks/infrastructure`;
los tipos de dominio en `tasks/domain`.

Constitución I: NO toca BrowserPort/SelectorRegistry/BrowserSession.
Constitución IV: fail-closed — claim_next no entrega dos veces el mismo item;
reconcile_stale re-encola huérfanos sin duplicar efecto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

# ---------------------------------------------------------------------------
# Domain value objects / enums
# ---------------------------------------------------------------------------


class WorkItemKind(StrEnum):
    """Clase de la unidad de trabajo (data-model.md §2, columna `kind`).

    `autonomous` = trabajo del loop del agente (default, retro-compat con P0).
    `chat_message` = mensaje del operador encolado via D-Bus (PIEZA 5 / US2).
    """

    AUTONOMOUS = "autonomous"
    CHAT_MESSAGE = "chat_message"


class TaskStatus(StrEnum):
    """Ciclo de vida de una unidad de trabajo (FR-004).

    Transiciones permitidas:
        PENDING        -> IN_PROGRESS            (claim_next)
        IN_PROGRESS    -> COMPLETED              (solo con AuditEntry de ejecución real, SC-001)
        IN_PROGRESS    -> FAILED                 (dispatch fallido | sin acciones)
        IN_PROGRESS    -> PENDING_APPROVAL       (HIGH sin hitl_approval_token)
        IN_PROGRESS    -> REJECTED               (rechazo por consent/política, terminal)
        FAILED         -> PENDING                (reintento con backoff, si attempts < max)
        PENDING_APPROVAL -> PENDING              (tras aprobación humana, re-dispatch)
        IN_PROGRESS    -> PENDING                (reconciliación de huérfano tras reinicio)
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class WorkItem:
    """Unidad de trabajo durable (Key Entity `agent_task`).

    Inmutable: cada transición produce una NUEVA instancia (no mutación).
    `payload`/`constraints`/`subjects` se traducen 1:1 a `DecisionContext`.

    Invariantes (FR-004, §Key Entities):
      - jamás dos estados a la vez (status es único).
      - COMPLETED solo con evidencia de ejecución real (impuesto en application).
      - sobrevive reinicios mientras no esté en estado terminal.
      - dedup_key vivo único (impuesto por índice UNIQUE parcial en infra).
    """

    id: UUID
    tenant_id: UUID
    trigger_kind: str  # p.ej. "manual_enqueue" (única fuente en P0)
    kind: WorkItemKind = WorkItemKind.AUTONOMOUS  # clase de la unidad (data-model §2)
    priority: int = 0  # mayor = más prioritario
    dedup_key: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    subjects: tuple[str, ...] = ()
    constraints: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = 3
    claim_token: UUID | None = None
    available_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @classmethod
    def new(
        cls,
        *,
        tenant_id: UUID,
        trigger_kind: str,
        payload: dict[str, Any],
        kind: WorkItemKind = WorkItemKind.AUTONOMOUS,
        priority: int = 0,
        dedup_key: str | None = None,
        subjects: tuple[str, ...] = (),
        constraints: dict[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> WorkItem:
        return cls(
            id=uuid4(),
            tenant_id=tenant_id,
            trigger_kind=trigger_kind,
            kind=kind,
            payload=payload,
            priority=priority,
            dedup_key=dedup_key,
            subjects=subjects,
            constraints=constraints or {},
            max_attempts=max_attempts,
        )


class AgentRunState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"


# ---------------------------------------------------------------------------
# Ports  (implemented in tasks/infrastructure)
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkQueuePort(Protocol):
    """Cola durable de unidades de trabajo (FR-001..FR-007).

    Implementación P0: SQLite sobre shell-state.db (WAL). Dequeue atómico
    por claim_token + lease; reconciliación de huérfanos tras reinicio.
    """

    async def enqueue(self, item: WorkItem) -> WorkItem:
        """Inserta un item PENDING. Idempotente por dedup_key (FR-005, SC-007):
        si ya existe un item vivo (no terminal) con la misma dedup_key,
        devuelve ESE item y no inserta uno nuevo (exactamente 1 ejecución).
        """
        ...

    async def claim_next(self) -> WorkItem | None:
        """Toma atómicamente el siguiente PENDING disponible por prioridad
        (FR-003). Marca IN_PROGRESS con claim_token + claimed_at + lease,
        incrementa attempts. Sin doble-toma (guard UPDATE ... WHERE
        status='pending'). Devuelve None si no hay trabajo (FR-010).
        """
        ...

    async def mark_completed(
        self,
        item_id: UUID,
        *,
        claim_token: UUID,
        audit_entry_id: UUID,
        execution_head_hash: str | None = None,
    ) -> None:
        """Transición a COMPLETED. PRECONDICIÓN (SC-001, FR-020): exige un
        audit_entry_id de ejecución real — la transición es inalcanzable sin
        él. Falla (raise) si claim_token no coincide (idempotencia anti doble
        cierre).
        execution_head_hash: hash real del audit chain para trazabilidad.
        """
        ...

    async def mark_failed(
        self, item_id: UUID, *, claim_token: UUID, reason: str
    ) -> WorkItem:
        """Transición a FAILED. Si attempts < max_attempts, re-programa a
        PENDING con backoff (available_at = now + base*2^attempts) para
        reintento idempotente (FR-006). Si no, FAILED terminal.
        """
        ...

    async def mark_pending_approval(
        self, item_id: UUID, *, claim_token: UUID, proposal_id: UUID
    ) -> None:
        """Transición a PENDING_APPROVAL (FR-015, US2). Sale del flujo de
        drenado; no bloquea el resto de la cola. La aprobación humana la
        re-encola (available_at=now).
        """
        ...

    async def mark_rejected(
        self, item_id: UUID, *, claim_token: UUID, reason: str
    ) -> None:
        """Transición terminal REJECTED (consent/política, fail-closed)."""
        ...

    async def reconcile_stale(self) -> int:
        """FR-007 / SC-003: re-encola (IN_PROGRESS con lease vencido) -> PENDING
        sin duplicar efecto. Llamado al arranque del daemon. Devuelve el nº
        reconciliado.
        """
        ...

    async def find_by_dedup_key(self, dedup_key: str) -> WorkItem | None:
        """Busca un item VIVO (no terminal) por dedup_key (SC-007)."""
        ...

    async def re_enqueue_after_approval(self, item_id: UUID) -> None:
        """PENDING_APPROVAL -> PENDING tras aprobación humana (FR-015).

        El operador aprobó la propuesta; el broker, al re-dispatchar la tarea,
        encontrará el token aprobado vía ApprovalGatePort.approved_token_for.

        Raises:
            ValueError: si el item no existe o no está en PENDING_APPROVAL.
        """
        ...

    async def renew_lease(self, item_id: UUID, *, claim_token: UUID) -> bool:
        """Renueva el lease de un item IN_PROGRESS si el claim_token coincide.

        Phase 2a: heartbeat para tareas largas de browser. Evita que
        reconcile_stale() re-encole una tarea que sigue activa (previene
        ejecución duplicada con su efecto lateral doble).

        Returns True si el lease fue renovado (el worker sigue siendo el dueño).
        Returns False si el item ya no pertenece a este worker (re-claimed,
        completado, o lease expirado y re-encolado). En ese caso el worker
        NO debe continuar ni llamar a mark_completed.

        Llamado periódicamente por el worker mientras procesa el item.
        El intervalo recomendado es lease_seconds / 3.
        """
        ...


@runtime_checkable
class AgentStatePort(Protocol):
    """Kill-switch / pausa persistente (FR-022..FR-024, US3, SC-005)."""

    async def is_paused(self) -> bool:
        """True si el agente está pausado. Consultado al inicio de cada
        vuelta del loop, antes de claim_next.
        """
        ...

    async def pause(self, *, by: UUID | None, reason: str) -> None:
        """Pausa: el loop deja de tomar trabajo e iniciar ejecuciones. La cola
        queda intacta. Transición observable y auditada (AGENT_PAUSED).
        """
        ...

    async def resume(self, *, by: UUID | None) -> None:
        """Reanuda sin pérdida ni duplicación. Auditada (AGENT_RESUMED)."""
        ...


@runtime_checkable
class TriggerSourcePort(Protocol):
    """Fuente de disparo de trabajo. En P0 SOLO encolado externo manual
    (§Decisiones: "tratar mensajes del operador como tareas = fase posterior").
    Se deja la abstracción para P2 (scheduler de timers/eventos, auto-encolado).
    """

    async def poll(self) -> tuple[WorkItem, ...]:
        """Devuelve items nuevos a encolar desde una fuente externa. P0: no-op
        o lectura de un buzón de inserción manual. P2: cron/eventos/D-Bus.
        """
        ...
