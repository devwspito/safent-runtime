"""TeachingSessionOrchestrator — thin orchestrator for isolated teaching (spec 004 / US3).

Composes:
  - TrainingSessionOrchestrator  (REUSED from spec 002)
  - TeachingContextFactory       (port; real adapter uses AgentBrowserCli --session)
  - InputOwnershipLedger         (in-memory poseedor único)

Responsibilities (narrow):
  - Open an isolated context for a teaching session (FR-003/FR-017).
  - Claim ownership for OPERATOR.
  - Reject opens whose isolation_key would collide with an already-registered
    execution key (FR-004, fail-closed).
  - Release the context on close/abandon.

  The TrainingSessionOrchestrator lifecycle (start/stop/sign) is driven
  exclusively by the router endpoints — open_teaching_session does NOT call
  _training.start() to avoid double-start overwrites (FR-003).

What it DOES NOT do:
  - Pause or modify active execution sessions (FR-017).
  - Enforce RBAC (caller's responsibility before invoking).
  - Perform any I/O beyond delegating to injected collaborators.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from hermes.agents_os.application.teaching.input_ownership_ledger import (
    InputOwnershipLedger,
)
from hermes.agents_os.application.teaching.ports import TeachingContextFactory
from hermes.agents_os.application.teaching.teaching_context import (
    InputOwner,
    InputOwnershipViolation,
    SurfaceKind,
    TeachingContext,
)
from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSessionOrchestrator,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TeachingSessionOpened:
    """Result returned by open_teaching_session."""

    teaching_session_id: UUID
    context: TeachingContext
    opened_at: datetime


class TeachingSessionOrchestrator:
    """Thin orchestrator: context isolation + ownership + training delegation."""

    def __init__(
        self,
        *,
        training_orchestrator: TrainingSessionOrchestrator,
        context_factory: TeachingContextFactory,
        ledger: InputOwnershipLedger,
        execution_isolation_keys: set[str] | None = None,
    ) -> None:
        self._training = training_orchestrator
        self._factory = context_factory
        self._ledger = ledger
        # Keys from active execution contexts registered externally.
        # Teaching MUST NOT collide with any of them (FR-004).
        self._exec_keys: set[str] = execution_isolation_keys or set()
        # context_id → TeachingContext for open sessions.
        self._open_contexts: dict[UUID, TeachingContext] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_teaching_session(
        self,
        *,
        teaching_session_id: UUID,
        surface_kind: SurfaceKind,
        tenant_id: UUID,
        operator_id: UUID,
        site_id: str,
    ) -> TeachingSessionOpened:
        """Open an isolated teaching context and claim OPERATOR ownership.

        Fail-closed: if the derived isolation_key collides with a registered
        execution key, raises InputOwnershipViolation (HTTP 409 at the boundary).

        Does NOT pause any execution session.
        Does NOT start the TrainingSessionOrchestrator — that is the sole
        responsibility of POST /training/{id}/start (FR-003: single creation
        point, consistent with the non-teaching flow).

        Returns:
            TeachingSessionOpened with the allocated context.

        Raises:
            InputOwnershipViolation: isolation_key collision with execution context.
        """
        ctx = self._factory.open(
            teaching_session_id=teaching_session_id,
            surface_kind=surface_kind,
            tenant_id=tenant_id,
            operator_id=operator_id,
            site_id=site_id,
        )
        self._assert_no_execution_collision(ctx)
        self._ledger.claim(ctx.context_id, InputOwner.OPERATOR)
        self._open_contexts[ctx.context_id] = ctx

        logger.info(
            "teaching_session.opened session_id=%s context_id=%s isolation_key=%s",
            teaching_session_id,
            ctx.context_id,
            ctx.isolation_key,
        )
        return TeachingSessionOpened(
            teaching_session_id=teaching_session_id,
            context=ctx,
            opened_at=datetime.now(tz=UTC),
        )

    def close_teaching_session(self, *, context_id: UUID) -> None:
        """Release ownership and tear down the isolated context.

        Safe to call even if the context was never opened (cleanup paths).
        """
        self._ledger.release(context_id)
        ctx = self._open_contexts.pop(context_id, None)
        if ctx is not None:
            self._factory.close(context_id)
            logger.info("teaching_session.closed context_id=%s", context_id)

    def register_execution_key(self, isolation_key: str) -> None:
        """Register an active execution context's isolation key.

        Called by execution-session management before an autonomous session
        starts; used to detect teach/execution collisions.
        """
        self._exec_keys.add(isolation_key)

    def deregister_execution_key(self, isolation_key: str) -> None:
        """Remove an execution context's key when it terminates."""
        self._exec_keys.discard(isolation_key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_no_execution_collision(self, ctx: TeachingContext) -> None:
        if ctx.isolation_key in self._exec_keys:
            raise InputOwnershipViolation(
                f"Teaching isolation_key '{ctx.isolation_key}' collides with an "
                "active execution context (FR-004). Open failed; no session paused."
            )
