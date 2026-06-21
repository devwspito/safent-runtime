"""TrainingSession (data-model §2, FR-006/035).

Una TrainingSession agrupa los StepRecords + VoiceNarrative producidos por
UN operador dentro de UN Workspace contra UN sitio target.

Ciclo de vida:

    started → operating → closing → compiled
                       → crashed (sin compilar)

- ``started``: el formador acaba de entrar al workspace, micrófono solicitado.
- ``operating``: capturando StepRecords activos.
- ``closing``: el formador pulsó "Finalizar"; pendientes transcripts en cola.
- ``compiled``: SkillPackage emitida.
- ``crashed``: VM perdió heartbeat antes de compilar.

Invariantes:
- ``tenant_id == workspace.tenant_id == operator.tenant_id``.
- ``mic_permission_granted`` se setea una sola vez, no se cambia.
- ``expires_at`` es un TTL agresivo (charter de confidencialidad): por defecto
  ``created_at + 4 h`` (ajustable).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4


class TrainingSessionState(StrEnum):
    STARTED = "started"
    OPERATING = "operating"
    CLOSING = "closing"
    COMPILED = "compiled"
    CRASHED = "crashed"


_ALLOWED: dict[TrainingSessionState, frozenset[TrainingSessionState]] = {
    TrainingSessionState.STARTED: frozenset(
        {TrainingSessionState.OPERATING, TrainingSessionState.CRASHED}
    ),
    TrainingSessionState.OPERATING: frozenset(
        {TrainingSessionState.CLOSING, TrainingSessionState.CRASHED}
    ),
    TrainingSessionState.CLOSING: frozenset(
        {TrainingSessionState.COMPILED, TrainingSessionState.CRASHED}
    ),
    TrainingSessionState.COMPILED: frozenset(),
    TrainingSessionState.CRASHED: frozenset(),
}


class TrainingSessionTransitionError(RuntimeError):
    """Transición de TrainingSessionState no permitida."""


def assert_training_transition(
    current: TrainingSessionState, target: TrainingSessionState
) -> None:
    if target not in _ALLOWED[current]:
        raise TrainingSessionTransitionError(
            f"Transición TrainingSession no permitida: {current} → {target}. "
            f"Permitidas desde {current}: {sorted(_ALLOWED[current])}"
        )


@dataclass(frozen=True, slots=True)
class TrainingSession:
    """Inmutable; mutaciones generan nuevas instancias via ``with_state``."""

    training_session_id: UUID = field(default_factory=uuid4)
    workspace_id: UUID | None = None
    tenant_id: UUID | None = None
    human_operator_id: UUID | None = None
    site_id: str = ""
    state: TrainingSessionState = TrainingSessionState.STARTED
    mic_permission_granted: bool | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    closed_at: datetime | None = None
    compiled_at: datetime | None = None
    expires_at: datetime | None = None
    step_count: int = 0
    runtime_version: str = ""
    skill_package_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.expires_at is None:
            # Default TTL agresivo: 4 horas desde started_at.
            ttl_hours = 4
            object.__setattr__(
                self, "expires_at", self.started_at + timedelta(hours=ttl_hours)
            )
        if self.step_count < 0:
            raise ValueError("step_count no puede ser negativo")

    def is_expired(self, *, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def with_state(
        self, new_state: TrainingSessionState, *, when: datetime | None = None
    ) -> TrainingSession:
        assert_training_transition(self.state, new_state)
        ts = when or datetime.now(tz=UTC)
        return TrainingSession(
            training_session_id=self.training_session_id,
            workspace_id=self.workspace_id,
            tenant_id=self.tenant_id,
            human_operator_id=self.human_operator_id,
            site_id=self.site_id,
            state=new_state,
            mic_permission_granted=self.mic_permission_granted,
            started_at=self.started_at,
            closed_at=ts if new_state == TrainingSessionState.CLOSING else self.closed_at,
            compiled_at=ts if new_state == TrainingSessionState.COMPILED else self.compiled_at,
            expires_at=self.expires_at,
            step_count=self.step_count,
            runtime_version=self.runtime_version,
            skill_package_id=self.skill_package_id,
        )
