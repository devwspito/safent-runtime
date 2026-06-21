"""AgentState value object — kill-switch / pausa del loop (FR-022/023/024).

Domain layer: puro, sin I/O, sin framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AgentState:
    """Estado de control del loop autónomo. Singleton por instancia de daemon.

    active  — el loop toma trabajo y lanza ejecuciones.
    paused  — el loop deja de tomar trabajo; la cola queda intacta.
    """

    is_paused: bool
    changed_by: UUID | None = None
    reason: str | None = None
    updated_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", datetime.now(tz=UTC))

    @classmethod
    def active(cls) -> AgentState:
        return cls(is_paused=False)

    @classmethod
    def paused(cls, *, by: UUID | None, reason: str) -> AgentState:
        return cls(is_paused=True, changed_by=by, reason=reason)
