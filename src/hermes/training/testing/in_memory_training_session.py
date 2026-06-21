"""InMemoryTrainingSession — testing-only (T102, constitución V).

Store en memoria de TrainingSession con comportamiento completo.
Multi-tenant strict: save y load verifican tenant_id.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from hermes.training.domain.training_session import TrainingSession, TrainingSessionState


class TrainingSessionNotFound(RuntimeError):
    """TrainingSession no existe o no pertenece al tenant."""


class InMemoryTrainingSession:
    """Store en memoria para tests. Thread-unsafe por diseño."""

    def __init__(self) -> None:
        # training_session_id → TrainingSession
        self._by_id: dict[UUID, TrainingSession] = {}

    async def save(self, session: TrainingSession) -> None:
        if session.tenant_id is None:
            raise ValueError("TrainingSession.tenant_id es requerido para save()")
        self._by_id[session.training_session_id] = session

    async def load(
        self,
        *,
        training_session_id: UUID,
        tenant_id: UUID,
    ) -> TrainingSession:
        s = self._by_id.get(training_session_id)
        if s is None or s.tenant_id != tenant_id:
            raise TrainingSessionNotFound(
                f"TrainingSession {training_session_id} no encontrada "
                f"para tenant {tenant_id}"
            )
        return s

    async def list_by_workspace(
        self,
        *,
        workspace_id: UUID,
        tenant_id: UUID,
    ) -> Sequence[TrainingSession]:
        return [
            s
            for s in self._by_id.values()
            if s.workspace_id == workspace_id and s.tenant_id == tenant_id
        ]

    async def list_by_state(
        self,
        *,
        tenant_id: UUID,
        state: TrainingSessionState,
    ) -> Sequence[TrainingSession]:
        return [
            s
            for s in self._by_id.values()
            if s.tenant_id == tenant_id and s.state == state
        ]

    def all_sessions(self) -> list[TrainingSession]:
        """Sin filtro de tenant — solo para assertions en tests."""
        return list(self._by_id.values())
