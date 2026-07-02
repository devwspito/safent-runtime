"""Máquina de estados pura de WorkItem — transiciones inmutables.

Cada función de transición recibe un WorkItem, valida guardas, y devuelve
una NUEVA instancia (no muta). Invariantes I1-I7 del data-model.md.

Domain layer: cero framework, cero agents_os, cero I/O.
"""

from __future__ import annotations

from dataclasses import fields as _dataclass_fields
from datetime import UTC, datetime, timedelta
from uuid import UUID

from hermes.tasks.domain.ports import TaskStatus, WorkItem

# Lease por defecto al hacer claim. El daemon lo configura desde env;
# aquí se define el valor dominio por defecto (60 s).
_DEFAULT_LEASE_SECONDS: int = 60

# Backoff base en segundos para reintentos (base * 2^attempt).
_BACKOFF_BASE_SECONDS: int = 30
_BACKOFF_CAP_SECONDS: int = 3600  # 1 hora máximo


class IllegalTransition(ValueError):
    """Transición de estado no permitida por la máquina de estados."""


def claim(item: WorkItem, *, claim_token: UUID) -> WorkItem:
    """PENDING -> IN_PROGRESS. Incrementa attempts, setea claim + lease (I3).

    Args:
        item: WorkItem en estado PENDING.
        claim_token: token único del intento de claim.

    Raises:
        IllegalTransition: si el item no está en PENDING.
    """
    if item.status is not TaskStatus.PENDING:
        raise IllegalTransition(
            f"claim solo desde PENDING; estado actual: {item.status!r}"
        )
    now = datetime.now(tz=UTC)
    return _replace(
        item,
        status=TaskStatus.IN_PROGRESS,
        attempts=item.attempts + 1,
        claim_token=claim_token,
        claimed_at=now,
        lease_expires_at=now + timedelta(seconds=_DEFAULT_LEASE_SECONDS),
    )


def mark_completed(
    item: WorkItem,
    *,
    claim_token: UUID,
    audit_entry_id: UUID,  # noqa: ARG001
) -> WorkItem:
    """IN_PROGRESS -> COMPLETED. Exige claim_token coincidente + audit_entry_id (I1/SC-001).

    Raises:
        IllegalTransition: si estado no es IN_PROGRESS o claim_token no coincide.
    """
    _assert_in_progress_with_token(item, claim_token, "mark_completed")
    # I2: terminal => sin claim/lease
    return _replace(
        item,
        status=TaskStatus.COMPLETED,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
    )


def mark_failed(
    item: WorkItem,
    *,
    claim_token: UUID,
    reason: str,
) -> WorkItem:
    """IN_PROGRESS -> FAILED (terminal) o PENDING (reintento con backoff).

    Si attempts < max_attempts: PENDING con available_at en el futuro (backoff).
    Si attempts >= max_attempts: FAILED terminal.

    Raises:
        IllegalTransition: si estado no es IN_PROGRESS o claim_token no coincide.
    """
    _assert_in_progress_with_token(item, claim_token, "mark_failed")

    if item.attempts < item.max_attempts:
        return _reschedule_with_backoff(item, reason)

    # Terminal: agota reintentos. I2: limpia claim/lease.
    return _replace(
        item,
        status=TaskStatus.FAILED,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
    )


def mark_pending_approval(
    item: WorkItem,
    *,
    claim_token: UUID,
    proposal_id: UUID,
) -> WorkItem:
    """IN_PROGRESS -> PENDING_APPROVAL. Libera lease para no bloquear cola (FR-024).

    Raises:
        IllegalTransition: si estado no es IN_PROGRESS o claim_token no coincide.
    """
    _assert_in_progress_with_token(item, claim_token, "mark_pending_approval")
    return _replace(
        item,
        status=TaskStatus.PENDING_APPROVAL,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
        payload={**item.payload, "_pending_proposal_id": str(proposal_id)},
    )


def mark_rejected(
    item: WorkItem,
    *,
    claim_token: UUID,
    reason: str,  # noqa: ARG001
) -> WorkItem:
    """IN_PROGRESS -> REJECTED (terminal, consent/política, fail-closed).

    Raises:
        IllegalTransition: si estado no es IN_PROGRESS o claim_token no coincide.
    """
    _assert_in_progress_with_token(item, claim_token, "mark_rejected")
    # I2: terminal => sin claim/lease
    return _replace(
        item,
        status=TaskStatus.REJECTED,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
    )


def mark_cancelled(
    item: WorkItem,
    *,
    claim_token: UUID,
    reason: str,
) -> WorkItem:
    """IN_PROGRESS -> CANCELLED (operador detuvo la tarea, terminal, SIN retry).

    A diferencia de mark_failed (que puede reencolar con backoff), una cancelación
    del operador es definitiva: no se reintenta.

    Raises:
        IllegalTransition: si estado no es IN_PROGRESS o claim_token no coincide.
    """
    _assert_in_progress_with_token(item, claim_token, "mark_cancelled")
    return _replace(
        item,
        status=TaskStatus.CANCELLED,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
        payload={**item.payload, "_cancel_reason": reason},
    )


def to_pending_after_approval(item: WorkItem) -> WorkItem:
    """PENDING_APPROVAL -> PENDING (tras aprobación humana, re-dispatch inmediato).

    Raises:
        IllegalTransition: si estado no es PENDING_APPROVAL.
    """
    if item.status is not TaskStatus.PENDING_APPROVAL:
        raise IllegalTransition(
            f"to_pending_after_approval solo desde PENDING_APPROVAL; estado: {item.status!r}"
        )
    now = datetime.now(tz=UTC)
    return _replace(
        item,
        status=TaskStatus.PENDING,
        available_at=now,
    )


def reconcile_to_pending(item: WorkItem) -> WorkItem:
    """IN_PROGRESS (lease vencido) -> PENDING — reconciliación boot (FR-007).

    Solo la capa de infraestructura debe invocar esto tras verificar lease vencido.

    Raises:
        IllegalTransition: si estado no es IN_PROGRESS.
    """
    if item.status is not TaskStatus.IN_PROGRESS:
        raise IllegalTransition(
            f"reconcile_to_pending solo desde IN_PROGRESS; estado: {item.status!r}"
        )
    return _replace(
        item,
        status=TaskStatus.PENDING,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
        available_at=datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _assert_in_progress_with_token(
    item: WorkItem, claim_token: UUID, op: str
) -> None:
    if item.status is not TaskStatus.IN_PROGRESS:
        raise IllegalTransition(
            f"{op} solo desde IN_PROGRESS; estado actual: {item.status!r}"
        )
    if item.claim_token != claim_token:
        raise IllegalTransition(
            f"{op}: claim_token no coincide — el lease puede haber vencido"
        )


def _reschedule_with_backoff(item: WorkItem, _reason: str) -> WorkItem:
    """Produce un WorkItem PENDING con available_at en backoff exponencial."""
    delay = min(
        _BACKOFF_BASE_SECONDS * (2 ** item.attempts),
        _BACKOFF_CAP_SECONDS,
    )
    available_at = datetime.now(tz=UTC) + timedelta(seconds=delay)
    return _replace(
        item,
        status=TaskStatus.PENDING,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
        available_at=available_at,
    )


def _replace(item: WorkItem, **kwargs: object) -> WorkItem:
    """Produce una nueva instancia de WorkItem con los campos sobreescritos.

    WorkItem es frozen+slots — no tiene __replace__ en Python 3.12 estándar.
    Usamos dataclasses.replace semántica manualmente para slots frozen.
    """
    current = {f.name: getattr(item, f.name) for f in _dataclass_fields(item)}
    current.update(kwargs)
    return WorkItem(**current)  # type: ignore[arg-type]
