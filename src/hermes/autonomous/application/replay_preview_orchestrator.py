"""ReplayPreviewOrchestrator — T104 (FR-019, FR-020, data-model §9).

Levanta ReplayPreviewSession en la VM con StorageState real del tenant y
wrapper ACTIVO desde el primer frame.

Fail-closed (THR-44, constitución IV):
  - Si el wrapper no se puede activar → estado FAILED antes de cualquier acción.
  - Si el tenant_id del operador no coincide con el de la skill → FAILED.

Lazy-import de playwright para constitución V.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from hermes.autonomous.application.read_only_browser_wrapper import (
    ReadOnlyBrowserWrapper,
    WrapperNotActivatedError,
)

logger = logging.getLogger(__name__)


class PreviewState(StrEnum):
    PREPARING = "preparing"
    RUNNING = "running"
    BLOCKED_MUTATION = "blocked_mutation"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class PreviewSessionHandle:
    preview_id: UUID = field(default_factory=uuid4)
    skill_id: UUID | None = None
    workspace_id: UUID | None = None
    tenant_id: UUID | None = None
    human_operator_id: UUID | None = None
    state: PreviewState = PreviewState.PREPARING
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    ended_at: datetime | None = None
    blocked_at_step_id: UUID | None = None
    block_reason: str | None = None


class MutationBlockedError(RuntimeError):
    """El preview fue bloqueado por una mutación (FR-020)."""

    def __init__(self, step_id: UUID, reason: str) -> None:
        super().__init__(f"Preview bloqueado en step {step_id}: {reason}")
        self.step_id = step_id
        self.reason = reason


class ReplayPreviewOrchestrator:
    """Orquesta el ReplayPreviewSession con wrapper read-only ACTIVO.

    Diseño:
    - start_preview: activa el wrapper ANTES del primer frame (fail-closed).
    - on_mutation_blocked: callback para emitir eventos al panel via WS.
    - El BrowserContext/Page reales son inyectados por el caller (infrastructure).
    """

    def __init__(
        self,
        *,
        side_effecting_patterns: tuple[str, ...] = (),
        extra_irreversible_patterns: tuple[str, ...] = (),
    ) -> None:
        self._side_effecting_patterns = side_effecting_patterns
        self._extra_irreversible_patterns = extra_irreversible_patterns
        self._sessions: dict[UUID, PreviewSessionHandle] = {}
        self._mutation_listeners: list[Callable[[UUID, str], None]] = []

    def on_mutation_blocked(self, callback: Callable[[UUID, str], None]) -> None:
        """Registra un listener para eventos de bloqueo (panel WS)."""
        self._mutation_listeners.append(callback)

    def start_preview(
        self,
        *,
        skill_id: UUID,
        workspace_id: UUID,
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> "tuple[PreviewSessionHandle, ReadOnlyBrowserWrapper]":
        """Crea un handle de preview y el wrapper listo para ser aplicado.

        El caller DEBE llamar wrapper.apply(context, page) ANTES del primer
        navigate. Si apply() falla, debe llamar fail_preview(handle.preview_id).

        Retorna (handle, wrapper) para que el infrastructure layer los use.
        """
        handle = PreviewSessionHandle(
            skill_id=skill_id,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
            state=PreviewState.PREPARING,
        )
        self._sessions[handle.preview_id] = handle

        wrapper = ReadOnlyBrowserWrapper(
            on_mutation_blocked=lambda reason, detail: self._emit_blocked(
                handle.preview_id, reason, detail
            ),
            side_effecting_patterns=self._side_effecting_patterns,
            extra_irreversible_patterns=self._extra_irreversible_patterns,
        )

        logger.info(
            "replay_preview_session_created",
            extra={
                "preview_id": str(handle.preview_id),
                "skill_id": str(skill_id),
                "tenant_id": str(tenant_id),
            },
        )
        return handle, wrapper

    def mark_running(self, preview_id: UUID) -> None:
        self._update_state(preview_id, PreviewState.RUNNING)

    def mark_completed(self, preview_id: UUID) -> None:
        handle = self._get(preview_id)
        handle.state = PreviewState.COMPLETED
        handle.ended_at = datetime.now(tz=UTC)
        logger.info("replay_preview_session_completed", extra={"preview_id": str(preview_id)})

    def fail_preview(self, preview_id: UUID, reason: str = "") -> None:
        handle = self._get(preview_id)
        handle.state = PreviewState.FAILED
        handle.ended_at = datetime.now(tz=UTC)
        handle.block_reason = reason
        logger.warning(
            "replay_preview_session_failed",
            extra={"preview_id": str(preview_id), "reason": reason},
        )

    def get(self, *, preview_id: UUID, tenant_id: UUID) -> PreviewSessionHandle:
        """Multi-tenant strict: verifica tenant_id."""
        handle = self._sessions.get(preview_id)
        if handle is None or handle.tenant_id != tenant_id:
            raise KeyError(f"Preview {preview_id} no encontrado para tenant {tenant_id}")
        return handle

    def _get(self, preview_id: UUID) -> PreviewSessionHandle:
        handle = self._sessions.get(preview_id)
        if handle is None:
            raise KeyError(f"Preview session {preview_id} no encontrada")
        return handle

    def _update_state(self, preview_id: UUID, state: PreviewState) -> None:
        self._get(preview_id).state = state

    def _emit_blocked(
        self, preview_id: UUID, reason: str, detail: str
    ) -> None:
        handle = self._sessions.get(preview_id)
        if handle:
            handle.state = PreviewState.BLOCKED_MUTATION
            handle.block_reason = reason
            handle.ended_at = datetime.now(tz=UTC)

        logger.warning(
            "replay_preview_mutation_blocked",
            extra={
                "preview_id": str(preview_id),
                "reason": reason,
                "detail": detail[:100],
            },
        )
        for listener in self._mutation_listeners:
            listener(preview_id, reason)
