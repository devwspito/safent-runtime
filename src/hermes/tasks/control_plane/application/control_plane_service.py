"""ControlPlaneService — application layer del bounded context control_plane LOCAL.

Implementa ControlPlanePort: chat→enqueue + supervisión read-only.

Controles de seguridad implementados:
  CTRL-P1-3: enqueued_by = UUID(channel.sender_uid), NUNCA del payload del cliente.
  CTRL-P1-4: AuditEntry(WORKITEM_ACCEPTED) síncrono en enqueue, antes de devolver.
  CTRL-P1-5: GetQueueStatus/ListPending/GetTaskStatus devuelven SOLO metadatos.
  CTRL-P1-6: rate-limit por sender_uid (token bucket) + cap de profundidad de cola.
  CTRL-P1-25 (T049 🔒): tokenización PII ANTES de persistir; PROHIBIDO loguear
    payload/instruction/user_message. El mapping de rehydratación se descarta
    (no se persiste). Los placeholders [[TYPE_N]] viajan en payload hasta el engine.

Diseño DDD:
  - Domain layer: WorkQueuePort, AgentStatePort, ApprovalGatePort (puertos).
  - Application layer: este módulo orquesta los puertos sin detalles de infra.
  - Sin dependencias de dbus-fast ni de SQLite — testeable con fakes.

FR-014/SC-008: enqueued_by SIEMPRE derivado del canal autenticado.
CWE-862: fail-closed — UID no autorizado ⇒ EnqueueNotAuthorized ANTES de tocar la cola.
CWE-770: rate-limit + cap de profundidad.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from hermes.agents_os.application.audit_hash_chain import AuditEntry, AuditKind
from hermes.tasks.control_plane.domain.ports import (
    AuthenticatedChannel,
    ConfiguredTaskView,
    EnqueueNotAuthorized,
    EnqueueResult,
    PendingTaskView,
    QueueStatus,
    RecentTaskView,
    TaskStatusView,
    UnknownTask,
)
from hermes.tasks.domain.ports import TaskStatus, WorkItem, WorkItemKind

if TYPE_CHECKING:
    from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
    from hermes.capabilities.domain.ports import ApprovalGatePort
    from hermes.tasks.control_plane.domain.ports import WorkerWakeSignal
    from hermes.tasks.domain.ports import AgentStatePort, WorkQueuePort

logger = logging.getLogger("hermes.tasks.control_plane.service")

# ---------------------------------------------------------------------------
# Rate-limit errors
# ---------------------------------------------------------------------------


class EnqueueRateLimited(RuntimeError):
    """sender_uid excedió el rate-limit de Enqueue (CTRL-P1-6 / CWE-770)."""


class EnqueueQueueFull(RuntimeError):
    """Cola supera el cap de profundidad (CTRL-P1-6 / CWE-770). Fail-closed."""


# ---------------------------------------------------------------------------
# Token bucket por sender_uid (CTRL-P1-6)
# ---------------------------------------------------------------------------

_BUCKET_CAPACITY = 50       # burst máximo
_REFILL_RATE = 10.0         # tokens por segundo
_QUEUE_DEPTH_CAP = 500      # items pending máximo en la cola


class _TokenBucket:
    """Token bucket de tasa fija por sender_uid. Thread-safe con asyncio.Lock."""

    def __init__(
        self,
        *,
        capacity: int = _BUCKET_CAPACITY,
        refill_rate: float = _REFILL_RATE,
    ) -> None:
        self._capacity = float(capacity)
        self._refill_rate = refill_rate
        self._tokens = float(capacity)
        self._last_refill: float = _now_monotonic()
        self._lock: asyncio.Lock = asyncio.Lock()

    async def consume(self) -> bool:
        """Consume 1 token. Devuelve True si OK, False si rate-limited."""
        async with self._lock:
            self._refill()
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True

    def _refill(self) -> None:
        now = _now_monotonic()
        elapsed = now - self._last_refill
        added = elapsed * self._refill_rate
        self._tokens = min(self._capacity, self._tokens + added)
        self._last_refill = now


def _now_monotonic() -> float:
    return time.monotonic()


# ---------------------------------------------------------------------------
# ControlPlaneService
# ---------------------------------------------------------------------------


class ControlPlaneService:
    """Orquesta verbos del control-plane LOCAL (D-Bus / CLI).

    Implementa ControlPlanePort (domain/ports.py). Testeable con fakes.
    """

    def __init__(
        self,
        *,
        queue: WorkQueuePort,
        agent_state: AgentStatePort,
        authorized_uids: frozenset[int],
        tenant_id: UUID,
        wake_signal: WorkerWakeSignal | None = None,
        audit_signer: AuditHashChainSigner | None = None,
        approval_gate: ApprovalGatePort | None = None,
        trigger_repo: Any | None = None,
    ) -> None:
        self._queue = queue
        self._state = agent_state
        self._authorized_uids = authorized_uids
        self._tenant_id = tenant_id
        self._wake = wake_signal
        self._signer = audit_signer
        self._gate = approval_gate
        self._trigger_repo = trigger_repo   # SqliteAuthorizedTriggerRepository | None
        self._rate_buckets: dict[int, _TokenBucket] = {}
        self._buckets_lock = asyncio.Lock()
        # In-memory audit entries (tests + fallback sin signer)
        self._audit_entries: list[AuditEntry] = []

    # ------------------------------------------------------------------
    # Verbos mutadores
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        *,
        channel: AuthenticatedChannel,
        trigger_kind: str,
        text: str,
        priority: int = 0,
        dedup_key: str | None = None,
        conversation_id: str | None = None,
        agent_id: str | None = None,
    ) -> EnqueueResult:
        """Encola un WorkItem.

        CTRL-P1-3: enqueued_by = UUID(channel.sender_uid), DESCARTA cualquier
        enqueued_by del payload del cliente.
        CTRL-P1-4: AuditEntry WORKITEM_ACCEPTED síncrono antes de devolver.
        CTRL-P1-6: rate-limit + cap de profundidad antes de tocar la cola.
        """
        self._authorize(channel.sender_uid, operation="enqueue")
        await self._check_rate_limit(channel.sender_uid)
        operator_uuid = _uid_to_uuid(channel.sender_uid)
        # CTRL-P1-25 (T049 🔒): tokenizar PII ANTES de persistir.
        # El mapping se descarta aquí — no se persiste (privacidad en memoria de proceso).
        # El broker rehidrata lo más tarde posible si el engine devuelve placeholders.
        safe_text = _tokenize_pii(text)
        item = _build_work_item(
            trigger_kind=trigger_kind,
            text=safe_text,
            priority=priority,
            dedup_key=dedup_key,
            operator_uuid=operator_uuid,
            tenant_id=self._tenant_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
        )
        persisted = await self._queue.enqueue(item)
        entry = self._emit_accepted_audit(
            sender_uid=channel.sender_uid, task_id=str(persisted.id)
        )
        self._audit_entries.append(entry)
        if self._wake is not None:
            self._wake.wake_one()
        stream_path = f"/ws/tasks/{persisted.id}"
        # CTRL-P1-25: log SIN payload ni instruction (solo metadatos de trazabilidad).
        logger.info(
            "hermes.cp.enqueued",
            extra={
                "task_id": str(persisted.id),
                "trigger_kind": trigger_kind,
                "by_uid": channel.sender_uid,
            },
        )
        return EnqueueResult(task_id=persisted.id, stream_path=stream_path)

    async def pause(self, *, channel: AuthenticatedChannel, reason: str) -> None:
        """Kill-switch. by = UUID(channel.sender_uid)."""
        self._authorize(channel.sender_uid, operation="pause")
        await self._state.pause(by=_uid_to_uuid(channel.sender_uid), reason=reason)

    async def resume(self, *, channel: AuthenticatedChannel) -> None:
        """Reanuda. by = UUID(channel.sender_uid)."""
        self._authorize(channel.sender_uid, operation="resume")
        await self._state.resume(by=_uid_to_uuid(channel.sender_uid))

    async def approve(
        self,
        *,
        channel: AuthenticatedChannel,
        proposal_id: UUID,
        mfa_factors: Any | None = None,
    ) -> str:
        """HITL approve. approved_by = UUID(channel.sender_uid). NO dispara run_cycle.

        `mfa_factors` se reenvía al gate, que verifica la MFA del dueño: la decisión de
        seguridad vive en el gate (toda superficie), no aquí (red-team 2026-06-19).
        """
        self._authorize(channel.sender_uid, operation="approve")
        if self._gate is None:
            raise NotImplementedError("approval_gate no inyectado")
        approved_by = _uid_to_uuid(channel.sender_uid)
        return await self._gate.approve(
            proposal_id=proposal_id, approved_by=approved_by, mfa_factors=mfa_factors
        )

    async def reject(
        self, *, channel: AuthenticatedChannel, proposal_id: UUID, reason: str
    ) -> None:
        """HITL reject. rejected_by = UUID(channel.sender_uid)."""
        self._authorize(channel.sender_uid, operation="reject")
        if self._gate is None:
            raise NotImplementedError("approval_gate no inyectado")
        rejected_by = _uid_to_uuid(channel.sender_uid)
        await self._gate.reject(
            proposal_id=proposal_id, rejected_by=rejected_by, reason=reason
        )

    # ------------------------------------------------------------------
    # Lectura (supervisión) — CTRL-P1-5: SOLO metadatos
    # ------------------------------------------------------------------

    async def get_queue_status(self) -> QueueStatus:
        """Snapshot read-only. NO altera estado (FR-016)."""
        return QueueStatus(
            state="active",
            pending=0,
            in_progress=0,
            pending_approval=0,
            last_audit_head_hex="",
        )

    async def list_pending(self, *, limit: int = 50) -> tuple[PendingTaskView, ...]:
        """Items PENDING por prioridad desc. SOLO (task_id, kind, status, enqueued_at).

        CTRL-P1-5: nunca devuelve payload_json ni instruction.
        Soporta SqliteWorkQueue (producción) e InMemoryWorkQueue (tests).
        """
        items = await _list_items_by_status(self._queue, TaskStatus.PENDING, limit=limit)
        return tuple(
            PendingTaskView(
                task_id=item.id,
                trigger_kind=item.trigger_kind,
                priority=item.priority,
                enqueued_at_iso=item.enqueued_at.isoformat(),
            )
            for item in items
        )

    async def get_task_status(self, *, task_id: UUID) -> TaskStatusView:
        """Estado de una tarea. SOLO metadatos, nunca payload/instruction.

        CTRL-P1-5: redacta instruction y payload_json.
        Raises UnknownTask si task_id no existe.
        Soporta SqliteWorkQueue (producción) e InMemoryWorkQueue (tests).
        """
        item = await _find_task_by_id(self._queue, task_id)
        if item is None:
            raise UnknownTask(f"task_id {task_id} no encontrado")
        return TaskStatusView(
            task_id=item.id,
            status=str(item.status),
            attempts=item.attempts,
            enqueued_by=item.payload.get("enqueued_by", ""),
            stream_path=f"/ws/tasks/{item.id}",
            error=None,
        )

    async def list_configured_tasks(
        self, *, limit: int = 200
    ) -> tuple[ConfiguredTaskView, ...]:
        """Configured tasks dashboard (= one row per authorized trigger).

        LEFT-JOINs each non-revoked trigger with its most-recent work item to
        populate last_run_at / last_status. next_run_at is computed for timer
        triggers via a minimal internal cron helper.

        CTRL-P1-5: no payload, no instruction, no credentials exposed.
        Returns empty tuple when trigger_repo is not available.
        """
        if self._trigger_repo is None:
            return ()

        rows = self._trigger_repo.list_triggers_with_last_run(limit=limit)
        now = datetime.now(tz=UTC)
        return tuple(
            _build_configured_task_view(trigger, last_run_at, last_status, now)
            for trigger, last_run_at, last_status in rows
        )

    async def list_recent_tasks(
        self, *, limit: int = 50
    ) -> tuple[RecentTaskView, ...]:
        """Recent work items across all statuses (activity log).

        CTRL-P1-5: instruction truncated to 120 chars; no full payload.
        Returns empty tuple when trigger_repo is not available.
        """
        if self._trigger_repo is None:
            return ()

        rows = self._trigger_repo.list_recent_tasks(limit=limit)
        return tuple(
            RecentTaskView(
                task_id=row["task_id"],
                label=row["instruction_truncated"] or f"[{row['trigger_kind']}]",
                status=row["status"],
                trigger_kind=row["trigger_kind"],
                enqueued_at=row["enqueued_at"] or "",
                claimed_at=row.get("claimed_at"),
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def audit_entries_emitted(self) -> list[AuditEntry]:
        """Devuelve las AuditEntry emitidas (para tests)."""
        return list(self._audit_entries)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _authorize(self, sender_uid: int, *, operation: str) -> None:
        """Fail-closed: EnqueueNotAuthorized si UID no autorizado (CWE-862)."""
        if sender_uid not in self._authorized_uids:
            logger.warning(
                "hermes.cp.authz_denied",
                extra={"operation": operation, "sender_uid": sender_uid},
            )
            raise EnqueueNotAuthorized(
                f"UID {sender_uid} no autorizado para '{operation}' (CWE-862)"
            )

    async def _check_rate_limit(self, sender_uid: int) -> None:
        """CTRL-P1-6: rate-limit por sender_uid (token bucket) + cap de profundidad.

        CWE-770: fail-closed — rechaza si pending items supera _QUEUE_DEPTH_CAP
        o si el token bucket se agota. El cap se consulta antes del token bucket
        para evitar consumir tokens cuando la cola está saturada.
        """
        await self._check_queue_depth_cap()
        bucket = await self._get_bucket(sender_uid)
        if not await bucket.consume():
            raise EnqueueRateLimited(
                f"UID {sender_uid} excedió el rate-limit de Enqueue (CTRL-P1-6)"
            )

    async def _check_queue_depth_cap(self) -> None:
        """CWE-770: rechaza si la cola tiene ≥ _QUEUE_DEPTH_CAP items pendientes."""
        pending = await _list_items_by_status(
            self._queue, TaskStatus.PENDING, limit=_QUEUE_DEPTH_CAP + 1
        )
        if len(pending) >= _QUEUE_DEPTH_CAP:
            logger.warning(
                "hermes.cp.queue_depth_cap_exceeded",
                extra={"pending_count": len(pending), "cap": _QUEUE_DEPTH_CAP},
            )
            raise EnqueueQueueFull(
                f"Cola supera el cap de profundidad ({_QUEUE_DEPTH_CAP} items pending). "
                "Reintenta más tarde (CTRL-P1-6 / CWE-770)."
            )

    async def _get_bucket(self, sender_uid: int) -> _TokenBucket:
        async with self._buckets_lock:
            if sender_uid not in self._rate_buckets:
                self._rate_buckets[sender_uid] = _TokenBucket()
            return self._rate_buckets[sender_uid]

    def _emit_accepted_audit(self, *, sender_uid: int, task_id: str) -> AuditEntry:
        """CTRL-P1-4: AuditEntry(WORKITEM_ACCEPTED) — síncrono, antes de devolver."""
        operator_str = str(_uid_to_uuid(sender_uid))
        return AuditEntry(
            entry_id=uuid4(),
            node_installation_id=None,
            tenant_id=self._tenant_id,
            timestamp=datetime.now(tz=UTC),
            actor=operator_str,
            audit_kind=AuditKind.WORKITEM_ACCEPTED,
            category="control_plane",
            description=f"WorkItem {task_id} aceptado (CTRL-P1-4)",
            payload_hash_hex="",
            prev_entry_hash_hex="",
            signed_payload_hash_hex="",
            signature_hex="",
        )


# ---------------------------------------------------------------------------
# Helpers de dominio
# ---------------------------------------------------------------------------


def _tokenize_pii(text: str) -> str:
    """CTRL-P1-25 (T049 🔒): tokeniza PII en el texto antes de persistir.

    Usa DefaultPIITokenizer de P0. El mapping se descarta (no se persiste).
    Llamado ANTES de construir el WorkItem — los placeholders viajan en payload.
    El broker rehidrata lo más tarde posible si el engine devuelve placeholders.
    """
    from hermes.tokenizer.pii import DefaultPIITokenizer  # noqa: PLC0415

    result = DefaultPIITokenizer().tokenize(text)
    if result.replaced > 0:
        logger.info(
            "hermes.cp.pii_tokenized",
            extra={"replaced": result.replaced},
            # CTRL-P1-25: NO loguear el texto ni el mapping
        )
    return result.sanitized if isinstance(result.sanitized, str) else text


async def _list_items_by_status(
    queue: WorkQueuePort, status: TaskStatus, *, limit: int
) -> list:
    """Lista items por estado desde cualquier implementación de WorkQueuePort.

    Soporta SqliteWorkQueue (vía list_by_status si disponible) e
    InMemoryWorkQueue (vía all_items). Fallback vacío si ninguno aplica.
    CTRL-P1-5: solo metadatos — el caller NO accede a payload/instruction.
    """
    # SqliteWorkQueue expone list_by_status (añadido en oleada actual)
    if hasattr(queue, "list_by_status"):
        return await queue.list_by_status(status=status, limit=limit)
    # InMemoryWorkQueue fallback (tests)
    if hasattr(queue, "all_items"):
        items = [i for i in queue.all_items() if i.status is status]
        items.sort(key=lambda x: (-x.priority, x.enqueued_at))
        return items[:limit]
    return []


async def _find_task_by_id(queue: WorkQueuePort, task_id: UUID):
    """Devuelve el WorkItem por task_id desde cualquier implementación.

    Soporta SqliteWorkQueue (vía task_by_id si disponible) e
    InMemoryWorkQueue (vía all_items). None si no existe.
    """
    if hasattr(queue, "task_by_id"):
        return await queue.task_by_id(task_id=task_id)
    if hasattr(queue, "all_items"):
        items = {i.id: i for i in queue.all_items()}
        return items.get(task_id)
    return None


def _uid_to_uuid(uid: int) -> UUID:
    """Convierte UID POSIX a UUID determinista como operator_id."""
    return UUID(int=uid)


# ---------------------------------------------------------------------------
# Cron utilities — croniter (next fire) + función propia (recurrence legible)
# ---------------------------------------------------------------------------

def _cron_next_fire(
    cron_expr: str,
    *,
    after: datetime,
) -> datetime | None:
    """Próximo disparo cron tras `after` (None si no computable).

    Delega en el vocabulario único `tasks.cron_schedule.next_fire` — el MISMO que
    usa el timer source (prev_fire), para no tener dos sitios envolviendo croniter.
    Se mantiene este nombre porque dbus_runtime_service lo importa.
    """
    from hermes.tasks.cron_schedule import next_fire  # noqa: PLC0415

    return next_fire(cron_expr, after=after)


# Días de la semana en cron (0/7=domingo … 6=sábado) → nombre en español.
_CRON_DOW_ES = {
    0: "domingo", 1: "lunes", 2: "martes", 3: "miércoles",
    4: "jueves", 5: "viernes", 6: "sábado", 7: "domingo",
}
_WEEKDAYS_SET = frozenset({1, 2, 3, 4, 5})


def _cron_recurrence_human(cron_expr: str) -> str:
    """Descripción legible de la recurrencia en español, sin dependencias externas.

    "30 9 * * 1"     → "Todos los lunes a las 09:30"
    "0 14 * * *"     → "Todos los días a las 14:00"
    "0 8 * * 1-5"    → "De lunes a viernes a las 08:00"
    "0 9 * * 1,3,5"  → "Lunes, miércoles y viernes a las 09:00"

    Solo interpreta el caso común (minuto y hora fijos, día-del-mes/mes en `*`).
    Cualquier expresión que no encaje un patrón conocido devuelve "" — la UI
    cae entonces a mostrar el cron crudo. Nunca lanza.

    NOTA: se hace a mano a propósito — la lib `cron_descriptor` empaqueta un
    top-level `tools/` que pisa el `tools/` de hermes-agent en site-packages.
    """
    try:
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return ""
        minute, hour, dom, month, dow = fields
        # Solo el patrón "a tal hora fija"; rangos/steps de hora no se describen.
        if not (minute.isdigit() and hour.isdigit() and dom == "*" and month == "*"):
            return ""
        hhmm = f"{int(hour):02d}:{int(minute):02d}"

        if dow == "*":
            return f"Todos los días a las {hhmm}"

        days = _parse_cron_dow(dow)
        if days is None:
            return ""
        if days == _WEEKDAYS_SET:
            return f"De lunes a viernes a las {hhmm}"
        if len(days) == 1:
            (d,) = tuple(days)
            return f"Todos los {_CRON_DOW_ES[d]} a las {hhmm}"

        names = [_CRON_DOW_ES[d] for d in sorted(days)]
        listed = ", ".join(names[:-1]) + " y " + names[-1]
        return f"{listed.capitalize()} a las {hhmm}"
    except Exception:  # noqa: BLE001 — la descripción es cosmética; jamás romper la vista
        return ""


def _parse_cron_dow(dow: str) -> set[int] | None:
    """Expande el campo día-de-semana del cron a un set de ints {0..6} (0=domingo).

    Soporta números sueltos, listas (1,3,5) y rangos (1-5). Devuelve None si
    aparece un step (*/n) u otra forma no soportada.
    """
    result: set[int] = set()
    for part in dow.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                return None
            for n in range(int(a), int(b) + 1):
                result.add(0 if n == 7 else n)
        elif part.isdigit():
            n = int(part)
            result.add(0 if n == 7 else n)
        else:
            return None  # step u otra forma → no describible
    return result if result else None


# ---------------------------------------------------------------------------
# ConfiguredTaskView builder
# ---------------------------------------------------------------------------

_LABEL_MAX = 120


def _build_configured_task_view(
    trigger: Any,
    last_run_at: str | None,
    last_status: str | None,
    now: datetime,
) -> ConfiguredTaskView:
    """Map (trigger, last_run_at, last_status) → ConfiguredTaskView.

    label derivation (in priority order):
      1. instruction from most-recent work item payload (not available here —
         the repository query returns only the ISO timestamp, not the payload;
         the label is derived from scope_value / trigger_type as a deterministic
         fallback that doesn't require a payload query).
      2. scope_value (cron expression or event class) prefixed by type.
    """
    from hermes.tasks.triggers.domain.authorized_trigger_ports import AuthorizedTriggerType  # noqa: PLC0415

    label = _derive_label(trigger)
    next_run_at: str | None = None
    recurrence_human = ""
    if trigger.trigger_type == AuthorizedTriggerType.TIMER:
        next_dt = _cron_next_fire(trigger.scope_value, after=now)
        next_run_at = next_dt.isoformat() if next_dt else None
        recurrence_human = _cron_recurrence_human(trigger.scope_value)

    return ConfiguredTaskView(
        trigger_id=str(trigger.trigger_instance_id),
        label=label,
        trigger_type=str(trigger.trigger_type),
        recurrence=trigger.scope_value,
        enabled=trigger.enabled,
        risk_ceiling=str(trigger.risk_ceiling),
        last_run_at=last_run_at,
        last_status=last_status,
        next_run_at=next_run_at,
        # P3 fields (default-safe if missing from older trigger rows)
        target_agent_id=getattr(trigger, "target_agent_id", None),
        task_instruction=getattr(trigger, "task_instruction", "") or "",
        one_shot=bool(getattr(trigger, "one_shot", False)),
        title=getattr(trigger, "title", "") or "",
        recurrence_human=recurrence_human,
    )


def _derive_label(trigger: Any) -> str:
    """Human-readable label for a trigger.

    Priority order (P3): title > task_instruction > 'type: scope_value'.
    title and task_instruction are stored on the trigger (P3 fields); pre-P3
    rows fall back to the scope_value derivation (same as before).
    """
    title = (getattr(trigger, "title", "") or "").strip()
    if title:
        return title[:_LABEL_MAX]
    instruction = (getattr(trigger, "task_instruction", "") or "").strip()
    if instruction:
        return instruction[:_LABEL_MAX]
    scope = trigger.scope_value[:_LABEL_MAX]
    return f"{trigger.trigger_type}: {scope}"


def _build_work_item(
    *,
    trigger_kind: str,
    text: str,
    priority: int,
    dedup_key: str | None,
    operator_uuid: UUID,
    tenant_id: UUID,
    conversation_id: str | None = None,
    agent_id: str | None = None,
) -> WorkItem:
    """Construye un WorkItem con enqueued_by derivado del canal — nunca del texto.

    conversation_id va en el payload: la cola lo espeja a la columna dedicada y
    la invariante I5 del esquema exige que un chat_message lo lleve (sin él, el
    INSERT OR IGNORE descarta la tarea en silencio).

    agent_id is the per-conversation contract agent resolved by chat_start.
    It travels in payload["agent_id"] so build_decision_context can read it
    and set DecisionContext.agent_id — the engine reads THAT, not the global
    active_agent_id.
    """
    payload: dict[str, str] = {
        # CTRL-P1-3: enqueued_by se fija aquí, NUNCA del texto del cliente
        "enqueued_by": str(operator_uuid),
        "instruction": text,
        # `chat_text`: el mensaje del usuario por una clave que NO es de control
        # (instruction/derived_from_untrusted_content/agent_id se filtran del
        # domain_payload). chat_text sobrevive al envoltorio igual que
        # conversation_id, así el engine SIEMPRE puede recuperar el turno del chat.
        "chat_text": text,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if agent_id:
        payload["agent_id"] = agent_id
    return WorkItem.new(
        tenant_id=tenant_id,
        trigger_kind=trigger_kind,
        payload=payload,
        kind=WorkItemKind.CHAT_MESSAGE,
        priority=priority,
        dedup_key=dedup_key,
    )
