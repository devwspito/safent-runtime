"""OtaOrchestrator — state machine OTA A/B con drain pre-promote + rollback.

Spec 003 Phase 2 T042 — cumple ``OtaUpdaterPort`` del contract.

State machine (data-model §6):

    queued → downloading → verifying ─invalid─→ rejected
                              │
                              ▼ (valid)
                        drain_in_progress → staged → booting_target
                                                          │
                                  ┌─healthy────┐         │
                                  ▼            ▼         │
                              promoted    rolled_back    │
                                                          │
                                                  aborted (admin manual)

Reglas BLOQUEANTES del threat-model (FR-050):

- **Monotonic versioning**: target_version DEBE ser estrictamente
  > previous_version (semver). Excepción: flag ``allow_downgrade=True``
  con motivo persistido en audit.
- **Revocation cache local**: lista firmada de versiones revocadas que
  se carga al boot del updater. Si target_version está en la lista
  → rejected con razón ``image_revoked``.
- **TTL revocation list**: si la lista no se refresca en > 30 días,
  el nodo entra en ``update_paused`` y notifica al admin (no aplica
  updates ciegamente).

Constitución IV (fail-closed): cualquier verificación que no pueda
dar respuesta definitiva = rechazo + audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

log = logging.getLogger(__name__)


class OtaAttemptState(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    DRAIN_IN_PROGRESS = "drain_in_progress"
    STAGED = "staged"
    BOOTING_TARGET = "booting_target"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"
    ABORTED = "aborted"


class OtaRejectionReason(StrEnum):
    SIGNATURE_INVALID = "signature_invalid"
    SIZE_BUDGET_EXCEEDED = "size_budget_exceeded"
    SBOM_MISSING = "sbom_missing"
    SBOM_MISMATCH = "sbom_mismatch"
    DISK_FULL = "disk_full"
    NETWORK_ERROR = "network_error"
    CLOCK_SKEW_SEVERE = "clock_skew_severe"
    IMAGE_REVOKED = "image_revoked"
    PROFILE_NOT_SUPPORTED = "profile_not_supported"
    DOWNGRADE_BLOCKED = "downgrade_blocked"
    REVOCATION_LIST_STALE = "revocation_list_stale"


class OtaRollbackReason(StrEnum):
    HEALTHY_TARGET_TIMEOUT = "healthy_target_timeout"
    KERNEL_PANIC = "kernel_panic"
    CRITICAL_SERVICE_FAILED = "critical_service_failed"
    MANUAL_ADMIN = "manual_admin"


class OtaError(RuntimeError):
    """Error operacional del orquestador."""


class OtaImageRejected(OtaError):
    """La imagen objetivo fue rechazada."""

    def __init__(self, reason: OtaRejectionReason, message: str = "") -> None:
        super().__init__(message or f"OTA rejected: {reason.value}")
        self.reason = reason


_ALLOWED_TRANSITIONS: dict[OtaAttemptState, frozenset[OtaAttemptState]] = {
    OtaAttemptState.QUEUED: frozenset(
        {OtaAttemptState.DOWNLOADING, OtaAttemptState.REJECTED, OtaAttemptState.ABORTED}
    ),
    OtaAttemptState.DOWNLOADING: frozenset(
        {OtaAttemptState.VERIFYING, OtaAttemptState.REJECTED, OtaAttemptState.ABORTED}
    ),
    OtaAttemptState.VERIFYING: frozenset(
        {OtaAttemptState.DRAIN_IN_PROGRESS, OtaAttemptState.REJECTED, OtaAttemptState.ABORTED}
    ),
    OtaAttemptState.DRAIN_IN_PROGRESS: frozenset(
        {OtaAttemptState.STAGED, OtaAttemptState.ABORTED}
    ),
    OtaAttemptState.STAGED: frozenset(
        {OtaAttemptState.BOOTING_TARGET, OtaAttemptState.ABORTED}
    ),
    OtaAttemptState.BOOTING_TARGET: frozenset(
        {OtaAttemptState.PROMOTED, OtaAttemptState.ROLLED_BACK}
    ),
    OtaAttemptState.PROMOTED: frozenset(),
    OtaAttemptState.ROLLED_BACK: frozenset(),
    OtaAttemptState.REJECTED: frozenset(),
    OtaAttemptState.ABORTED: frozenset(),
}


class OtaStateTransitionError(RuntimeError):
    """Transición no permitida en la state machine."""


def assert_transition(current: OtaAttemptState, target: OtaAttemptState) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise OtaStateTransitionError(
            f"Transición OTA no permitida: {current} → {target}. "
            f"Permitidas desde {current}: {sorted(_ALLOWED_TRANSITIONS[current])}"
        )


@dataclass(slots=True)
class OtaUpdateAttempt:
    """Mutable; refleja la fila en BD."""

    attempt_id: UUID
    node_installation_id: UUID
    target_image_version: str
    target_image_digest: str
    from_image_version: str
    state: OtaAttemptState = OtaAttemptState.QUEUED
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    verified_at: datetime | None = None
    staged_at: datetime | None = None
    promote_attempted_at: datetime | None = None
    concluded_at: datetime | None = None
    rejection_reason: OtaRejectionReason | None = None
    rollback_reason: OtaRollbackReason | None = None
    runs_paused_count: int = 0
    runs_completed_during_drain_count: int = 0
    training_sessions_persisted_count: int = 0
    remote_operators_notified_count: int = 0


# ---------------------------------------------------------------------------
# Monotonic versioning + revocation cache (FR-050 BLOCKER)
# ---------------------------------------------------------------------------


def parse_semver(version: str) -> tuple[int, int, int]:
    """Parser pragmático de semver — acepta ``vX.Y.Z`` o ``X.Y.Z``."""
    v = version
    for prefix in ("agents-os-", "v"):
        if v.startswith(prefix):
            v = v[len(prefix) :]
    parts = v.split(".")
    if len(parts) < 3:
        raise ValueError(f"version {version!r} no es semver válido (X.Y.Z requerido)")
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2].split("-")[0]))
    except ValueError as exc:
        raise ValueError(f"version {version!r} no es semver válido") from exc


def is_strict_upgrade(target: str, current: str) -> bool:
    """True si ``target`` es estrictamente mayor que ``current``."""
    return parse_semver(target) > parse_semver(current)


@dataclass(frozen=True, slots=True)
class RevocationList:
    """Lista firmada de versiones revocadas — FR-050."""

    revoked_versions: frozenset[str]
    refreshed_at: datetime
    signature_hex: str
    ttl_days: int = 30

    def is_stale(self, *, now: datetime) -> bool:
        return (now - self.refreshed_at) > timedelta(days=self.ttl_days)

    def is_revoked(self, version: str) -> bool:
        return version in self.revoked_versions


# ---------------------------------------------------------------------------
# OtaOrchestrator
# ---------------------------------------------------------------------------


class OtaOrchestrator:
    """State machine + monotonic versioning + revocation cache + drain.

    Inyectables:
        always_on_supervisor: para invocar ``drain_for_ota()``.
        revocation_list: lista firmada cargada al boot.

    Diseño: el orchestrator coordina la state machine; las operaciones
    concretas (download, cosign verify, bootc stage, reboot) las delega
    a un ``BootcUpdater`` adapter (infrastructure).
    """

    def __init__(
        self,
        *,
        revocation_list: RevocationList | None = None,
        always_on_supervisor: Any | None = None,
        bootc_updater: Any | None = None,
        clock: Any = datetime,
    ) -> None:
        self._revocation = revocation_list
        self._supervisor = always_on_supervisor
        self._bootc = bootc_updater
        self._clock = clock
        self._attempts: dict[UUID, OtaUpdateAttempt] = {}

    # ------------------------------------------------------------------
    # Pre-flight checks (FR-050 BLOQUEANTES)
    # ------------------------------------------------------------------

    def _check_monotonic(
        self,
        *,
        target_version: str,
        current_version: str,
        allow_downgrade: bool,
    ) -> None:
        if allow_downgrade:
            log.warning(
                "ota.allow_downgrade_used",
                extra={"target": target_version, "current": current_version},
            )
            return
        if not is_strict_upgrade(target_version, current_version):
            raise OtaImageRejected(
                OtaRejectionReason.DOWNGRADE_BLOCKED,
                f"target {target_version} <= current {current_version}; "
                "downgrade requiere allow_downgrade=True + motivo.",
            )

    def _check_revocation(
        self,
        *,
        target_version: str,
        now: datetime,
    ) -> None:
        if self._revocation is None:
            # Sin lista cargada — fail-closed (constitución IV).
            raise OtaImageRejected(
                OtaRejectionReason.REVOCATION_LIST_STALE,
                "revocation list no cargada — fail-closed",
            )
        if self._revocation.is_stale(now=now):
            raise OtaImageRejected(
                OtaRejectionReason.REVOCATION_LIST_STALE,
                f"revocation list stale (TTL {self._revocation.ttl_days}d "
                f"excedido). Nodo en update_paused hasta refresh.",
            )
        if self._revocation.is_revoked(target_version):
            raise OtaImageRejected(
                OtaRejectionReason.IMAGE_REVOKED,
                f"version {target_version} está en revocation list firmada",
            )

    # ------------------------------------------------------------------
    # Public API (cumple OtaUpdaterPort)
    # ------------------------------------------------------------------

    def queue_attempt(
        self,
        *,
        node_installation_id: UUID,
        target_image_version: str,
        target_image_digest: str,
        from_image_version: str,
        allow_downgrade: bool = False,
    ) -> OtaUpdateAttempt:
        """Crea un ``OtaUpdateAttempt`` en estado QUEUED.

        Aplica las verificaciones BLOQUEANTES de FR-050 ANTES de tocar
        el slot B. Si alguna falla → REJECTED + audit, sin descarga.
        """
        now = datetime.now(tz=UTC)
        attempt = OtaUpdateAttempt(
            attempt_id=uuid4(),
            node_installation_id=node_installation_id,
            target_image_version=target_image_version,
            target_image_digest=target_image_digest,
            from_image_version=from_image_version,
            started_at=now,
        )
        self._attempts[attempt.attempt_id] = attempt

        try:
            self._check_monotonic(
                target_version=target_image_version,
                current_version=from_image_version,
                allow_downgrade=allow_downgrade,
            )
            self._check_revocation(target_version=target_image_version, now=now)
        except OtaImageRejected as exc:
            self._reject(attempt, exc.reason)
            return attempt

        return attempt

    def transition(
        self,
        attempt: OtaUpdateAttempt,
        new_state: OtaAttemptState,
        *,
        rejection_reason: OtaRejectionReason | None = None,
        rollback_reason: OtaRollbackReason | None = None,
    ) -> OtaUpdateAttempt:
        """Transición controlada con auditoría implícita."""
        assert_transition(attempt.state, new_state)
        attempt.state = new_state
        now = datetime.now(tz=UTC)

        if new_state == OtaAttemptState.VERIFYING:
            pass  # se marca verified_at al pasar a DRAIN_IN_PROGRESS
        elif new_state == OtaAttemptState.DRAIN_IN_PROGRESS:
            attempt.verified_at = now
        elif new_state == OtaAttemptState.STAGED:
            attempt.staged_at = now
        elif new_state == OtaAttemptState.BOOTING_TARGET:
            attempt.promote_attempted_at = now
        elif new_state == OtaAttemptState.PROMOTED:
            attempt.concluded_at = now
        elif new_state == OtaAttemptState.ROLLED_BACK:
            attempt.concluded_at = now
            attempt.rollback_reason = rollback_reason
        elif new_state == OtaAttemptState.REJECTED:
            attempt.concluded_at = now
            attempt.rejection_reason = rejection_reason
        elif new_state == OtaAttemptState.ABORTED:
            attempt.concluded_at = now
        return attempt

    def _reject(
        self, attempt: OtaUpdateAttempt, reason: OtaRejectionReason
    ) -> None:
        """Atajo para rejections desde QUEUED (no pasa por DOWNLOADING)."""
        attempt.state = OtaAttemptState.REJECTED
        attempt.rejection_reason = reason
        attempt.concluded_at = datetime.now(tz=UTC)
        log.warning(
            "ota.rejected",
            extra={
                "attempt_id": str(attempt.attempt_id),
                "reason": reason.value,
                "target": attempt.target_image_version,
            },
        )

    def get_attempt(self, attempt_id: UUID) -> OtaUpdateAttempt | None:
        return self._attempts.get(attempt_id)
