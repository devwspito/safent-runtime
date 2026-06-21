"""Puertos del bounded context LOCAL `control_plane` — feature 006 / PIEZA 5.

Source of truth de las firmas del plano de control LOCAL daemon<->shell/CLI.
DDD: este es un bounded context DISTINTO del remoto (spec 002, workspace/,
mTLS/JSON-RPC VM->CP). NO se reutiliza ni se mezcla.

Capa: control_plane/domain define los puertos (Protocols) + value objects.
      control_plane/infrastructure los implementa (adapter D-Bus dbus-fast +
      socket Unix de stream). control_plane/application orquesta el chat->enqueue.

Constitucion I: NO toca BrowserPort/SelectorRegistry/BrowserSession/StorageStatePort.
Constitucion IV: fail-closed. enqueue/comandos sin authZ valida -> NO ejecuta.
FR-014/SC-008: enqueued_by deriva del canal autenticado (sender_uid del bus),
NUNCA del payload del cliente.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

# ---------------------------------------------------------------------------
# Errores (fail-closed)
# ---------------------------------------------------------------------------


class ControlPlaneError(RuntimeError):
    """Base del plano de control local."""


class AgentUnavailable(ControlPlaneError):
    """El daemon/D-Bus no esta disponible. El chat FALLA explicito (FR-012,
    SC-005). NO hay fallback passthrough."""


class EnqueueNotAuthorized(ControlPlaneError):
    """sender_uid del canal autenticado no autorizado a encolar (FR-015,
    fail-closed). Queda traza de la negacion."""


class UnknownTask(ControlPlaneError):
    """task_id inexistente."""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class StreamChunkKind(StrEnum):
    """Tipos del stream de tarea (espejo del protocolo del socket WS)."""

    DELTA = "delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_CALL = "tool_call"
    STATUS = "status"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TaskStreamChunk:
    """Un fragmento del stream de una tarea. Inmutable."""

    kind: StreamChunkKind
    delta: str = ""
    tool_call: dict[str, Any] | None = None
    status: str | None = None
    outcome: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Resultado de encolar. `stream_path` = ruta del socket de chunks."""

    task_id: UUID
    stream_path: str  # p.ej. "/ws/tasks/<task_id>"


@dataclass(frozen=True, slots=True)
class QueueStatus:
    """Snapshot read-only de la cola (FR-016)."""

    state: str  # "active" | "paused"
    pending: int
    in_progress: int
    pending_approval: int
    last_audit_head_hex: str


@dataclass(frozen=True, slots=True)
class PendingTaskView:
    """Vista read-only de un item PENDING (supervision)."""

    task_id: UUID
    trigger_kind: str
    priority: int
    enqueued_at_iso: str


@dataclass(frozen=True, slots=True)
class TaskStatusView:
    """Estado observable de una tarea (re-attach tras stream interrumpido)."""

    task_id: UUID
    status: str
    attempts: int
    enqueued_by: str  # UUID derivado del sender_uid (autoria inalterable)
    stream_path: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ConfiguredTaskView:
    """Vista read-only de una tarea CONFIGURADA (= un trigger autorizado).

    Dashboard row: un trigger + datos de su ejecución más reciente.
    CTRL-P1-5: SOLO metadatos — nunca payload/instruction/credenciales.

    Campos P3 (scheduled-tasks): opcionales con default para no romper
    consumidores que construyan la vista sin los campos nuevos.
    """

    trigger_id: str          # UUID del authorized_trigger_instances
    label: str               # etiqueta legible (instrucción truncada o fallback)
    trigger_type: str        # 'timer' | 'system_event' | 'self_enqueue'
    recurrence: str          # scope_value del trigger (cron expr o event class)
    enabled: bool            # kill-switch del origen
    risk_ceiling: str        # 'low' | 'high'
    last_run_at: str | None  # ISO; most-recent work item enqueued_at
    last_status: str | None  # 'completed' | 'failed' | 'in_progress' | ...
    next_run_at: str | None  # ISO; próxima ejecución (solo timer; None si no computable)
    # ── P3: campos de calendario por-agente ──────────────────────────────────
    target_agent_id: str | None = None  # agente destino; None = activo en el momento
    task_instruction: str = ""          # instrucción almacenada en el trigger
    one_shot: bool = False              # True = auto-revoca tras primera ejecución
    title: str = ""                     # etiqueta legible del calendario (UI)
    recurrence_human: str = ""          # descripción legible del cron (UI; "" si no aplica/falla)


@dataclass(frozen=True, slots=True)
class RecentTaskView:
    """Vista read-only de una ejecución reciente de cualquier work item.

    Activity-log row. CTRL-P1-5: nunca payload/instruction completo.
    """

    task_id: str            # UUID de agent_tasks
    label: str              # instrucción truncada a 120 chars o fallback
    status: str             # 'pending' | 'in_progress' | 'completed' | 'failed' | ...
    trigger_kind: str       # 'timer' | 'chat_message' | 'manual_enqueue' | ...
    enqueued_at: str        # ISO
    claimed_at: str | None  # ISO; None si aún PENDING


@dataclass(frozen=True, slots=True)
class AuthenticatedChannel:
    """Identidad del canal autenticado del plano de control (FR-014/NFR-003).

    `sender_uid` lo resuelve el ADAPTER desde el bus (GetConnectionUnixUser),
    NUNCA es un parametro que el cliente pueda falsificar. `enqueued_by` se
    deriva de aqui, jamas del payload del mensaje.
    """

    sender_uid: int


# ---------------------------------------------------------------------------
# Puerto principal — plano de control LOCAL (verbos)
# ---------------------------------------------------------------------------


@runtime_checkable
class ControlPlanePort(Protocol):
    """Puerta del operador a la cola + supervision (FR-008..FR-018).

    Adapter real: cliente D-Bus org.hermes.Runtime1 (dbus-fast) en
    control_plane/infrastructure. El shell-server y el CLI lo consumen.
    El daemon expone el lado servidor via el mismo contrato D-Bus.

    AuthZ: TODO metodo mutador exige `channel` cuyo sender_uid el ADAPTER
    resolvio del bus. enqueued_by deriva de channel.sender_uid (FR-014).
    Fail-closed (FR-015): sin authZ valida -> EnqueueNotAuthorized.
    Sin daemon -> AgentUnavailable (FR-012, sin fallback).
    """

    async def enqueue(
        self,
        *,
        channel: AuthenticatedChannel,
        trigger_kind: str,
        text: str,
        priority: int = 0,
        dedup_key: str | None = None,
    ) -> EnqueueResult:
        """Encola un WorkItem. `enqueued_by` = UUID(channel.sender_uid),
        NUNCA del contenido (FR-014/SC-008). Despierta el loop inmediato
        (wake-on-enqueue, FR-013/SC-006). Idempotente por dedup_key vivo.

        Raises:
            EnqueueNotAuthorized: sender_uid no autorizado (fail-closed).
            AgentUnavailable: daemon/D-Bus caido (sin fallback).
        """
        ...

    async def get_queue_status(self) -> QueueStatus:
        """Snapshot read-only (FR-016). No altera estado del agente."""
        ...

    async def list_pending(self, *, limit: int = 50) -> tuple[PendingTaskView, ...]:
        """Items PENDING por prioridad desc (supervision read-only)."""
        ...

    async def get_task_status(self, *, task_id: UUID) -> TaskStatusView:
        """Estado de una tarea (re-attach). Raises UnknownTask si no existe."""
        ...

    async def pause(self, *, channel: AuthenticatedChannel, reason: str) -> None:
        """Kill-switch. `by` = UUID(channel.sender_uid). Raises
        EnqueueNotAuthorized si no autorizado."""
        ...

    async def resume(self, *, channel: AuthenticatedChannel) -> None:
        """Reanuda. `by` = UUID(channel.sender_uid)."""
        ...

    async def approve(
        self, *, channel: AuthenticatedChannel, proposal_id: UUID
    ) -> str:
        """HITL approve. `approved_by` = UUID(channel.sender_uid). NO dispara
        run_cycle (NFR-001); el loop re-dispatcha. Devuelve approval_token."""
        ...

    async def reject(
        self, *, channel: AuthenticatedChannel, proposal_id: UUID, reason: str
    ) -> None:
        """HITL reject. `rejected_by` = UUID(channel.sender_uid)."""
        ...


# ---------------------------------------------------------------------------
# Puerto del stream de chunks (socket Unix de tareas)
# ---------------------------------------------------------------------------


@runtime_checkable
class TaskStreamPort(Protocol):
    """Consume el stream de chunks de UNA tarea (FR-009).

    Adapter real: cliente WS sobre socket Unix /run/hermes/tasks.sock en
    control_plane/infrastructure. El shell lo consume tras recibir
    EnqueueResult.stream_path. Propiedad del daemon; PII viaja por canal
    local autorizado (NFR-007), NUNCA por el provider.
    """

    async def subscribe(self, *, stream_path: str) -> AsyncIterator[TaskStreamChunk]:
        """Itera los chunks de la tarea. Re-attach permitido: si la tarea ya
        termino, emite STATUS + DONE actuales (no replay del histgrico).
        """
        ...


# ---------------------------------------------------------------------------
# Puerto del sink que el loop usa para EMITIR chunks (lado daemon)
# ---------------------------------------------------------------------------


@runtime_checkable
class ChunkSinkPort(Protocol):
    """El loop/engine EMITE chunks por aqui (lado productor, en el daemon).

    Se inyecta por-tarea via DecisionContext.metadata['chunk_sink'] para NO
    modificar la firma publica de ReasoningEngine.run_cycle (Constitucion I).
    El adapter publica al socket de stream de esa task_id. Best-effort: un
    cliente lento NO bloquea el ciclo (la verdad durable es el audit).
    """

    async def emit(self, *, task_id: UUID, chunk: TaskStreamChunk) -> None:
        """Publica un chunk al stream de `task_id`. No bloqueante por cliente
        lento (descarta deltas si el buffer esta lleno)."""
        ...

    async def close(self, *, task_id: UUID, outcome: str, error: str | None = None) -> None:
        """Cierra el stream de la tarea con un frame DONE."""
        ...


# ---------------------------------------------------------------------------
# Wake-on-enqueue — primitiva compartida loop<->control-plane (FR-013/NFR-002)
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkerWakeSignal(Protocol):
    """Senal in-process que despierta a UN worker libre al encolar (NFR-002).

    Abstraida desde ya para que el paso de mono-worker a POOL (PIEZA 4) NO
    obligue a re-refactorizar la senalizacion (Assumption firmada). Implementacion
    mono: asyncio.Event. Implementacion pool: asyncio.Condition/semaforo.
    """

    def wake_one(self) -> None:
        """Despierta a un worker en idle. Idempotente (varios wake = un drain).
        Llamado por ControlPlanePort.enqueue tras commit del item en la cola.
        """
        ...

    async def wait_for_work(self, *, timeout: float) -> bool:  # noqa: ASYNC109
        """Bloquea hasta wake_one() o timeout. True si hubo wake, False si
        timeout (vuelta de poll ociosa). Reemplaza el asyncio.sleep ciego del
        _idle() actual.
        """
        ...
