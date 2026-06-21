"""HealthyTargetWatchdog — vigila el boot del slot target tras OTA.

Spec 003 FR-008 + FR-050 BLOQUEANTE. Tras `bootc reboot`, el sistema
arranca el slot inactivo. Si `agents-os-healthy.target` no se alcanza
en `healthy_target_timeout_sec` (default 600s), disparamos rollback
automático.

Esta clase NO bloquea — espera asíncronamente eventos del target. El
adapter `systemd_event_subscriber` traduce eventos del bus al callback
`mark_target_reached()`.

Estados:
  WAITING → TARGET_REACHED  (PROMOTED)
  WAITING → TIMEOUT          (ROLLBACK)
  WAITING → ABORTED          (manual o emergencia)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

_DEFAULT_TIMEOUT_SEC = 600  # 10 min


class WatchdogState(StrEnum):
    WAITING = "waiting"
    TARGET_REACHED = "target_reached"
    TIMEOUT = "timeout"
    ABORTED = "aborted"


class WatchdogError(RuntimeError):
    pass


class WatchdogStateInvalid(WatchdogError):
    pass


@runtime_checkable
class RollbackPort(Protocol):
    """Puerto contra el OtaOrchestrator / BootcUpdater para auto-rollback."""

    def rollback(self, *, attempt_id: UUID, reason: str) -> None: ...


@dataclass(slots=True)
class HealthyTargetWatcher:
    """Watcher de un único OTA attempt en curso."""

    attempt_id: UUID
    target_image_version: str
    started_at: datetime
    timeout_seconds: int = _DEFAULT_TIMEOUT_SEC
    state: WatchdogState = WatchdogState.WAITING
    target_reached_at: datetime | None = None
    timed_out_at: datetime | None = None
    aborted_at: datetime | None = None

    @property
    def deadline(self) -> datetime:
        return self.started_at + timedelta(seconds=self.timeout_seconds)


class HealthyTargetWatchdog:
    """Mantiene los watchers activos y dispara rollback al expirar."""

    def __init__(
        self,
        *,
        rollback_port: RollbackPort,
        clock=lambda: datetime.now(tz=UTC),
        default_timeout_seconds: int = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self._rollback = rollback_port
        self._clock = clock
        self._default_timeout = default_timeout_seconds
        self._watchers: dict[UUID, HealthyTargetWatcher] = {}

    def begin_watching(
        self,
        *,
        attempt_id: UUID,
        target_image_version: str,
        timeout_seconds: int | None = None,
    ) -> HealthyTargetWatcher:
        if attempt_id in self._watchers:
            raise WatchdogStateInvalid(
                f"watcher para {attempt_id} ya existe"
            )
        watcher = HealthyTargetWatcher(
            attempt_id=attempt_id,
            target_image_version=target_image_version,
            started_at=self._clock(),
            timeout_seconds=timeout_seconds or self._default_timeout,
        )
        self._watchers[attempt_id] = watcher
        return watcher

    def mark_target_reached(self, *, attempt_id: UUID) -> HealthyTargetWatcher:
        watcher = self._fetch(attempt_id)
        if watcher.state != WatchdogState.WAITING:
            raise WatchdogStateInvalid(
                f"watcher en estado {watcher.state}"
            )
        watcher.state = WatchdogState.TARGET_REACHED
        watcher.target_reached_at = self._clock()
        return watcher

    def check_timeouts(self) -> list[HealthyTargetWatcher]:
        """Revisión periódica — dispara rollback en los expirados.

        Retorna la lista de watchers que se marcaron TIMEOUT en esta
        invocación (para que el caller los persista en audit).
        """
        now = self._clock()
        triggered = []
        for watcher in list(self._watchers.values()):
            if watcher.state != WatchdogState.WAITING:
                continue
            if now >= watcher.deadline:
                watcher.state = WatchdogState.TIMEOUT
                watcher.timed_out_at = now
                self._rollback.rollback(
                    attempt_id=watcher.attempt_id,
                    reason="healthy_target_timeout",
                )
                triggered.append(watcher)
        return triggered

    def abort_watching(self, *, attempt_id: UUID, reason: str) -> HealthyTargetWatcher:
        watcher = self._fetch(attempt_id)
        if watcher.state != WatchdogState.WAITING:
            return watcher  # idempotente si ya está en estado terminal
        watcher.state = WatchdogState.ABORTED
        watcher.aborted_at = self._clock()
        return watcher

    def get_watcher(self, *, attempt_id: UUID) -> HealthyTargetWatcher:
        return self._fetch(attempt_id)

    def active_watchers(self) -> tuple[HealthyTargetWatcher, ...]:
        return tuple(
            w for w in self._watchers.values()
            if w.state == WatchdogState.WAITING
        )

    def _fetch(self, attempt_id: UUID) -> HealthyTargetWatcher:
        if attempt_id not in self._watchers:
            raise WatchdogStateInvalid(
                f"watcher {attempt_id} no existe"
            )
        return self._watchers[attempt_id]
