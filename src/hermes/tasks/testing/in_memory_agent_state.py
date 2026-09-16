"""InMemoryAgentState — fake de AgentStatePort para tests unitarios."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from hermes.tasks.domain.ports import AgentStatePort


class InMemoryAgentState:
    """Implementación en memoria de AgentStatePort para tests."""

    def __init__(self, *, paused: bool = False) -> None:
        self._paused = paused
        self._reason: str | None = None
        self._changed_by: UUID | None = None
        self._changed_at: str | None = None
        self.pause_calls: list[dict] = []
        self.resume_calls: list[dict] = []

    async def is_paused(self) -> bool:
        return self._paused

    async def pause(
        self, *, by: UUID | None, reason: str, provenance: str = ""
    ) -> None:
        self._paused = True
        self._reason = reason
        self._changed_by = by
        self._changed_at = datetime.now(tz=UTC).isoformat()
        self.pause_calls.append({"by": by, "reason": reason, "provenance": provenance})

    async def resume(self, *, by: UUID | None, reason: str = "") -> None:
        self._paused = False
        self._reason = None
        self._changed_by = by
        self._changed_at = datetime.now(tz=UTC).isoformat()
        self.resume_calls.append({"by": by, "reason": reason})

    async def status(self) -> dict:
        return {
            "engaged": self._paused,
            "reason": self._reason,
            "changed_by": str(self._changed_by) if self._changed_by else None,
            "changed_at": self._changed_at,
        }


# Satisface AgentStatePort structural check
assert isinstance(InMemoryAgentState(), AgentStatePort)
