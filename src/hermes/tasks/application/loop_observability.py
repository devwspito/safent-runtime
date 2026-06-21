"""Observabilidad del loop autónomo — audit + structured logging (T026/FR-019/FR-021).

Emite:
- Audit entry TASK_CLAIMED/TASK_COMPLETED/TASK_FAILED vía SignedAuditRepositoryPort.
- Logs structlog SIN PII: task_id, trigger_kind, status, transición, audit_entry_id.

El caller (AgentLoopOrchestrator) pasa el firmer ya sembrado; aquí solo
coordinamos la emisión. Sin PII: no se loguean payload, subjects ni constraints.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner, AuditKind

logger = logging.getLogger("hermes.tasks.loop")


async def emit_task_claimed(
    *,
    firmer: AuditHashChainSigner,
    audit_repo: Any,
    task_id: UUID,
    tenant_id: UUID,
    trigger_kind: str,
    actor: str = "agent-loop",
) -> UUID:
    """Emite TASK_CLAIMED en la cadena de audit. Returns audit_entry_id."""
    entry = await firmer.append_and_persist(
        audit_kind=AuditKind.TASK_CLAIMED,
        actor=actor,
        description=f"Task {task_id} claimed — trigger: queue_drain:{trigger_kind}",
        payload={"task_id": str(task_id), "trigger_kind": trigger_kind},
        tenant_id=tenant_id,
        audit_repo=audit_repo,
    )

    logger.info(
        "hermes.tasks.loop.task_claimed",
        extra={
            "task_id": str(task_id),
            "trigger_kind": trigger_kind,
            "audit_entry_id": str(entry.entry_id),
        },
    )
    return entry.entry_id


async def emit_task_completed(
    *,
    firmer: AuditHashChainSigner,
    audit_repo: Any,
    task_id: UUID,
    tenant_id: UUID,
    execution_audit_entry_id: UUID,
    actor: str = "agent-loop",
) -> UUID:
    """Emite TASK_COMPLETED en la cadena de audit."""
    entry = await firmer.append_and_persist(
        audit_kind=AuditKind.TASK_COMPLETED,
        actor=actor,
        description=f"Task {task_id} completed",
        payload={
            "task_id": str(task_id),
            "execution_audit_entry_id": str(execution_audit_entry_id),
        },
        tenant_id=tenant_id,
        audit_repo=audit_repo,
    )

    logger.info(
        "hermes.tasks.loop.task_completed",
        extra={
            "task_id": str(task_id),
            "execution_audit_entry_id": str(execution_audit_entry_id),
            "audit_entry_id": str(entry.entry_id),
        },
    )
    return entry.entry_id


async def emit_chat_replied(
    *,
    firmer: AuditHashChainSigner,
    audit_repo: Any,
    task_id: UUID,
    tenant_id: UUID,
    narrative_len: int,
    actor: str = "agent-loop",
) -> tuple[UUID, str]:
    """Emite CHAT_REPLIED en la cadena de audit. Returns (audit_entry_id, head_hash_hex).

    `narrative_len` es la longitud del texto — NUNCA el texto en claro (PII/CTRL-P1-9).
    `head_hash_hex` es firmer.head_hash_hex DESPUÉS de firmar esta entrada, lo que
    lo convierte en la evidencia de ejecución real requerida por SC-001.
    """
    entry = await firmer.append_and_persist(
        audit_kind=AuditKind.CHAT_REPLIED,
        actor=actor,
        description=f"Task {task_id} — chat replied without tool calls",
        payload={"task_id": str(task_id), "narrative_len": narrative_len},
        tenant_id=tenant_id,
        audit_repo=audit_repo,
    )

    logger.info(
        "hermes.tasks.loop.chat_replied",
        extra={
            "task_id": str(task_id),
            "narrative_len": narrative_len,
            "audit_entry_id": str(entry.entry_id),
        },
    )
    return entry.entry_id, firmer.head_hash_hex


async def emit_task_failed(
    *,
    firmer: AuditHashChainSigner,
    audit_repo: Any,
    task_id: UUID,
    tenant_id: UUID,
    reason: str,
    actor: str = "agent-loop",
) -> UUID:
    """Emite TASK_FAILED en la cadena de audit. No loguea PII del reason."""
    entry = await firmer.append_and_persist(
        audit_kind=AuditKind.TASK_FAILED,
        actor=actor,
        description=f"Task {task_id} failed",
        payload={"task_id": str(task_id)},
        tenant_id=tenant_id,
        audit_repo=audit_repo,
    )

    logger.warning(
        "hermes.tasks.loop.task_failed",
        extra={
            "task_id": str(task_id),
            "reason": reason,
            "audit_entry_id": str(entry.entry_id),
        },
    )
    return entry.entry_id
