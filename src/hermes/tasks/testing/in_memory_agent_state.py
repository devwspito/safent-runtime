"""InMemoryAgentState — fake de AgentStatePort para tests unitarios."""

from __future__ import annotations

from uuid import UUID

from hermes.tasks.domain.ports import AgentStatePort


class InMemoryAgentState:
    """Implementación en memoria de AgentStatePort para tests."""

    def __init__(self, *, paused: bool = False) -> None:
        self._paused = paused
        self.pause_calls: list[dict] = []
        self.resume_calls: list[dict] = []

    async def is_paused(self) -> bool:
        return self._paused

    async def pause(self, *, by: UUID | None, reason: str) -> None:
        self._paused = True
        self.pause_calls.append({"by": by, "reason": reason})

    async def resume(self, *, by: UUID | None) -> None:
        self._paused = False
        self.resume_calls.append({"by": by})


# Satisface AgentStatePort structural check
assert isinstance(InMemoryAgentState(), AgentStatePort)
