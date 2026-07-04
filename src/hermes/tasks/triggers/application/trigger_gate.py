"""TriggerGate — pre-gate default-deny (US2, FR-015, CTRL-P2-7/8/9).

EL CORAZÓN de la feature 007: la ÚNICA puerta por la que una fuente
automática puede poner trabajo en la cola. Las trigger sources NO reciben
WorkQueuePort directamente — solo esta fachada (lint del grafo FR-014).

Flujo fail-closed (FR-015):
  1. is_authorized(type, scope) → None  ⇒  TRIGGER_DENIED + no encola + None.
  2. Si self_enqueue: valida cascade_depth ≤ 0 en tarea madre y consume_budget.
  3. enqueued_by = trigger.created_by_admin_uuid (timer/system_event)
     o el enqueued_by de la tarea madre (self_enqueue). NUNCA NULL/'system'.
     Si no se puede derivar → fail-closed (CTRL-P2-8/CWE-862).
  4. Tokeniza PII, construye WorkItem(trigger_kind=tipo, ...), queue.enqueue.
  5. Audit TRIGGER_ACTIVATED encadenado. commit-then-wake.

Reutiliza _tokenize_pii de ControlPlaneService (no duplica).
NO modifica el contrato del broker (FR-028/Constitución I).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from hermes.agents_os.application.audit_hash_chain import AuditEntry, AuditKind
from hermes.tasks.domain.ports import WorkItem, WorkItemKind
from hermes.tasks.triggers.domain.authorized_trigger_ports import (
    AuthorizedTriggerType,
)

if TYPE_CHECKING:
    from hermes.tasks.domain.ports import AgentStatePort, WorkQueuePort
    from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (
        SqliteAuthorizedTriggerRepository,
    )

logger = logging.getLogger("hermes.tasks.triggers.gate")


class TriggerGate:
    """Único punto de encolado para fuentes automáticas (FR-015/CTRL-P2-7).

    Las tres TriggerSources deben llamar a enqueue_from_trigger — NUNCA
    a WorkQueuePort.enqueue directamente.
    """

    def __init__(
        self,
        *,
        trigger_repo: SqliteAuthorizedTriggerRepository,
        queue: WorkQueuePort,
        agent_state: AgentStatePort,
        tenant_id: UUID,
        audit_signer: Any | None = None,  # AuditHashChainSigner (no-repudio real)
        audit_repo: Any | None = None,    # repo durable con async append(entry)
    ) -> None:
        self._repo = trigger_repo
        self._queue = queue
        self._state = agent_state
        self._tenant_id = tenant_id
        # P0-6: con signer+repo, TRIGGER_DENIED/ACTIVATED se firman y persisten en
        # la hash-chain (no-repudio durable). Sin ellos (tests) cae a una lista en
        # memoria con entradas sin firmar.
        self._audit_signer = audit_signer
        self._audit_repo = audit_repo
        self._audit_entries: list[AuditEntry] = []

    async def _record_audit(
        self, *, audit_kind: AuditKind, actor: str, description: str, payload: dict
    ) -> None:
        if self._audit_signer is not None and self._audit_repo is not None:
            entry = await self._audit_signer.append_and_persist(
                audit_kind=audit_kind,
                actor=actor,
                description=description,
                payload=payload,
                audit_repo=self._audit_repo,
                tenant_id=self._tenant_id,
                category="triggers",
            )
            self._audit_entries.append(entry)
            return
        # Fallback (tests sin signer): entrada NO firmada en memoria.
        self._audit_entries.append(
            AuditEntry(
                entry_id=uuid4(),
                node_installation_id=None,
                tenant_id=self._tenant_id,
                timestamp=datetime.now(tz=UTC),
                actor=actor,
                audit_kind=audit_kind,
                category="triggers",
                description=description,
                payload_hash_hex="",
                prev_entry_hash_hex="",
                signed_payload_hash_hex="",
                signature_hex="",
            )
        )

    # ------------------------------------------------------------------
    # Pública: única puerta de encolado para fuentes automáticas
    # ------------------------------------------------------------------

    async def enqueue_from_trigger(
        self,
        *,
        trigger_type: AuthorizedTriggerType,
        scope_value: str,
        instruction: str,
        dedup_key: str | None = None,
        priority: int = 0,
        derived_from_untrusted_content: bool = False,
        parent_work_item_id: UUID | None = None,
        target_agent_id: str | None = None,
        kind: WorkItemKind = WorkItemKind.AUTONOMOUS,
        conversation_id: str | None = None,
        delegation_correlation_id: str | None = None,
    ) -> UUID | None:
        """Flujo fail-closed (FR-015). Devuelve task_id o None si rechazado.

        target_agent_id (opcional): agente destino de una tarea programada del
        calendario. Si está, viaja en el payload del WorkItem y el consumidor de
        la cola lo ejecuta con ESE agente (routing per-agent).

        kind/conversation_id (FASE 3 A2A cross-human, EXTERNAL_DELEGATION): un
        origen puede pedir kind=WorkItemKind.CHAT_MESSAGE para que la tarea entre
        por el mismo carril de respuesta que un chat normal (I5 del esquema exige
        conversation_id NOT NULL para chat_message — fail-closed aquí ANTES de
        tocar la cola, en vez de dejar que el CHECK de SQLite lo descubra tarde).
        Por defecto kind=AUTONOMOUS/conversation_id=None — CERO cambio de
        comportamiento para timer/system_event/self_enqueue.

        delegation_correlation_id (EXTERNAL_DELEGATION): correlation_id de la
        DelegationEnvelope que originó esta tarea — viaja en el payload para que,
        al completarse, el pusher de resultados (config_sync.delegation_inbox)
        sepa a qué correlation_id devolver el resultado (POST /v1/outbox/result).
        """
        if kind is WorkItemKind.CHAT_MESSAGE and not conversation_id:
            logger.warning(
                "hermes.triggers.gate.chat_message_missing_conversation_id",
                extra={"trigger_type": str(trigger_type)},
            )
            await self._emit_denied(trigger_type=trigger_type, scope_value=scope_value)
            return None

        # Paso 1 — consulta la allow-list (NO cacheada, CTRL-P2-15)
        trigger = await self._repo.is_authorized(
            trigger_type=trigger_type,
            scope_value=scope_value,
        )
        if trigger is None:
            await self._emit_denied(trigger_type=trigger_type, scope_value=scope_value)
            return None

        # Paso 2 — validaciones específicas por tipo
        enqueued_by = await self._resolve_enqueued_by(
            trigger_type=trigger_type,
            trigger_admin=trigger.created_by_admin_uuid,
            parent_work_item_id=parent_work_item_id,
        )
        if enqueued_by is None:
            await self._emit_denied(trigger_type=trigger_type, scope_value=scope_value)
            return None

        if trigger_type is AuthorizedTriggerType.SELF_ENQUEUE:
            ok = await self._validate_self_enqueue(
                trigger_instance_id=trigger.trigger_instance_id,
                dedup_key=dedup_key,
                parent_work_item_id=parent_work_item_id,
            )
            if not ok:
                await self._emit_denied(trigger_type=trigger_type, scope_value=scope_value)
                return None

        # Paso 3 — tokeniza PII (CTRL-P2-12/NFR-006)
        safe_instruction = _tokenize_pii(instruction)

        # Paso 4 — construye el WorkItem con atribución de autoría correcta
        item = self._build_work_item(
            trigger_type=trigger_type,
            trigger_instance_id=trigger.trigger_instance_id,
            instruction=safe_instruction,
            enqueued_by=enqueued_by,
            priority=priority,
            dedup_key=dedup_key,
            derived_from_untrusted_content=derived_from_untrusted_content,
            parent_work_item_id=parent_work_item_id,
            target_agent_id=target_agent_id,
            kind=kind,
            conversation_id=conversation_id,
            delegation_correlation_id=delegation_correlation_id,
        )

        # Paso 5 — encola (idempotente por dedup_key)
        persisted = await self._queue.enqueue(item)

        # Paso 6 — audit TRIGGER_ACTIVATED
        await self._emit_activated(
            trigger_type=trigger_type,
            trigger_instance_id=trigger.trigger_instance_id,
            task_id=persisted.id,
            admin_uuid=trigger.created_by_admin_uuid,
        )

        logger.info(
            "hermes.triggers.gate.activated",
            extra={
                "trigger_type": str(trigger_type),
                "scope_value": scope_value,
                "task_id": str(persisted.id),
                "instance_id": str(trigger.trigger_instance_id),
            },
        )
        return persisted.id

    def audit_entries(self) -> list[AuditEntry]:
        """Devuelve las AuditEntry emitidas (para tests)."""
        return list(self._audit_entries)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _resolve_enqueued_by(
        self,
        *,
        trigger_type: AuthorizedTriggerType,
        trigger_admin: UUID,
        parent_work_item_id: UUID | None,
    ) -> UUID | None:
        """Deriva enqueued_by fail-closed (CTRL-P2-8/FR-016/CWE-862).

        timer / system_event → admin que autorizó el origen.
        self_enqueue → enqueued_by de la tarea madre (nunca del contenido).
        """
        if trigger_type is AuthorizedTriggerType.SELF_ENQUEUE:
            if parent_work_item_id is None:
                logger.warning(
                    "hermes.triggers.gate.no_parent_for_self_enqueue",
                    extra={"trigger_type": str(trigger_type)},
                )
                return None
            parent = await self._find_work_item(parent_work_item_id)
            if parent is None:
                logger.warning(
                    "hermes.triggers.gate.parent_not_found",
                    extra={"parent_id": str(parent_work_item_id)},
                )
                return None
            raw = parent.payload.get("enqueued_by")
            if not raw:
                return None
            try:
                return UUID(str(raw))
            except (ValueError, AttributeError):
                return None

        # timer / system_event → admin del origen firmado
        return trigger_admin

    async def _validate_self_enqueue(
        self,
        *,
        trigger_instance_id: UUID,
        dedup_key: str | None,
        parent_work_item_id: UUID | None,
    ) -> bool:
        """Valida cap de cascada, dedup obligatoria, y presupuesto/hora."""
        # dedup_key obligatoria (FR-022)
        if not dedup_key:
            logger.warning("hermes.triggers.gate.self_enqueue_missing_dedup_key")
            return False

        # Cap de cascada = 1 (SC-007): la tarea madre NO debe ser ya self_enqueue
        if parent_work_item_id is not None:
            parent = await self._find_work_item(parent_work_item_id)
            if parent is not None:
                cascade_depth = parent.payload.get("cascade_depth", 0)
                if int(cascade_depth) >= 1:
                    logger.warning(
                        "hermes.triggers.gate.cascade_depth_exceeded",
                        extra={"cascade_depth": cascade_depth},
                    )
                    return False

        # Presupuesto por hora (CTRL-P2-10)
        has_budget = await self._repo.consume_budget(
            trigger_instance_id=trigger_instance_id
        )
        if not has_budget:
            logger.warning(
                "hermes.triggers.gate.hourly_budget_exhausted",
                extra={"instance_id": str(trigger_instance_id)},
            )
            return False

        return True

    async def _find_work_item(self, item_id: UUID) -> WorkItem | None:
        """Busca un WorkItem en la cola (cualquier estado)."""
        if hasattr(self._queue, "all_items"):
            for item in self._queue.all_items():
                if item.id == item_id:
                    return item
        if hasattr(self._queue, "task_by_id"):
            return await self._queue.task_by_id(task_id=item_id)
        return None

    def _build_work_item(
        self,
        *,
        trigger_type: AuthorizedTriggerType,
        trigger_instance_id: UUID,
        instruction: str,
        enqueued_by: UUID,
        priority: int,
        dedup_key: str | None,
        derived_from_untrusted_content: bool,
        parent_work_item_id: UUID | None,
        target_agent_id: str | None = None,
        kind: WorkItemKind = WorkItemKind.AUTONOMOUS,
        conversation_id: str | None = None,
        delegation_correlation_id: str | None = None,
    ) -> WorkItem:
        """Construye el WorkItem con atribución completa (FR-016/CTRL-P2-8)."""
        cascade_depth = 1 if trigger_type is AuthorizedTriggerType.SELF_ENQUEUE else 0
        payload: dict = {
            # CTRL-P2-8: enqueued_by = admin autorizador o madre; NUNCA NULL/'system'
            "enqueued_by": str(enqueued_by),
            "instruction": instruction,
            "trigger_instance_id": str(trigger_instance_id),
            "cascade_depth": cascade_depth,
            "derived_from_untrusted_content": derived_from_untrusted_content,
        }
        if parent_work_item_id is not None:
            payload["parent_work_item_id"] = str(parent_work_item_id)
        # Routing per-agent (calendario de tareas): el agente destino viaja en la
        # clave `agent_id` del payload — el carril que el DecisionContextBuilder
        # ya lee (payload["agent_id"] → DecisionContext.agent_id; None = activo).
        # Así la tarea programada se ejecuta con ESE agente, no con el activo en
        # el momento del disparo.
        if target_agent_id:
            payload["agent_id"] = target_agent_id
        # kind=CHAT_MESSAGE (EXTERNAL_DELEGATION): mismas claves que
        # ControlPlaneService._build_work_item para que el motor/engine encuentre
        # el turno de chat igual que uno manual (I5 ya validada por el caller).
        if kind is WorkItemKind.CHAT_MESSAGE:
            payload["conversation_id"] = conversation_id
            payload["chat_text"] = instruction
        # correlation_id de la DelegationEnvelope — leído por config_sync.
        # delegation_inbox.push_pending_delegation_results_once al completarse la
        # tarea, para devolver el resultado a A vía POST /v1/outbox/result.
        if delegation_correlation_id:
            payload["delegation_correlation_id"] = delegation_correlation_id

        return WorkItem.new(
            tenant_id=self._tenant_id,
            trigger_kind=str(trigger_type),
            payload=payload,
            kind=kind,
            priority=priority,
            dedup_key=dedup_key,
        )

    async def _emit_denied(
        self,
        *,
        trigger_type: AuthorizedTriggerType,
        scope_value: str,
    ) -> None:
        """Registra TRIGGER_DENIED (FR-015/CTRL-P2-7/SC-001) en la hash-chain."""
        await self._record_audit(
            audit_kind=AuditKind.TRIGGER_DENIED,
            actor="trigger_gate",
            description=(
                f"Origen denegado — tipo={trigger_type} scope={scope_value} "
                "(allow-list vacía o origen revocado — CTRL-P2-7)"
            ),
            payload={
                "trigger_type": str(trigger_type),
                "scope_value": scope_value,
            },
        )
        logger.warning(
            "hermes.triggers.gate.denied",
            extra={"trigger_type": str(trigger_type), "scope_value": scope_value},
        )

    async def _emit_activated(
        self,
        *,
        trigger_type: AuthorizedTriggerType,
        trigger_instance_id: UUID,
        task_id: UUID,
        admin_uuid: UUID,
    ) -> None:
        """Registra TRIGGER_ACTIVATED (CTRL-P2-14/FR-025) en la hash-chain."""
        await self._record_audit(
            audit_kind=AuditKind.TRIGGER_ACTIVATED,
            actor=str(admin_uuid),
            description=(
                f"Origen activado — tipo={trigger_type} "
                f"instance={trigger_instance_id} task={task_id}"
            ),
            payload={
                "trigger_type": str(trigger_type),
                "trigger_instance_id": str(trigger_instance_id),
                "task_id": str(task_id),
            },
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tokenize_pii(text: str) -> str:
    """CTRL-P2-12/NFR-006: tokeniza PII antes de persistir. Reutiliza P0."""
    from hermes.tokenizer.pii import (  # noqa: PLC0415
        DefaultPIITokenizer,
        actionable_pii_exclusions,
    )

    result = DefaultPIITokenizer(
        exclude_patterns=actionable_pii_exclusions()
    ).tokenize(text)
    if result.replaced > 0:
        logger.info("hermes.triggers.gate.pii_tokenized", extra={"replaced": result.replaced})
    return result.sanitized if isinstance(result.sanitized, str) else text
