"""LLM Budget per TrainingSession — BLOCKER CTRL-8 (T098).

Counter de invocaciones al DecisionRuleInferencer por sesión.
Superado el límite → training_session pasa a FAILED con razón
`llm_budget_exceeded`.

Configuración:
  - max_calls: número máximo de llamadas (default 100).
  - max_usd: gasto máximo en USD (default 1.0).
  - cost_per_call_usd: coste estimado por llamada (default 0.01).

El budget se evalúa antes de cada llamada al inferencer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CALLS = 100
_DEFAULT_MAX_USD = 1.0
_DEFAULT_COST_PER_CALL_USD = 0.01

BUDGET_EXCEEDED_REASON = "llm_budget_exceeded"


class LlmBudgetExceeded(RuntimeError):
    """Se superó el presupuesto LLM de la sesión de training (CTRL-8)."""

    def __init__(self, tenant_id: UUID, training_session_id: UUID, reason: str) -> None:
        super().__init__(
            f"LLM budget exceeded for session {training_session_id} "
            f"(tenant {tenant_id}): {reason}"
        )
        self.tenant_id = tenant_id
        self.training_session_id = training_session_id
        self.reason = reason


@dataclass
class SessionBudgetState:
    calls_made: int = 0
    usd_spent: float = 0.0


class LlmBudgetCounter:
    """Gestiona el budget LLM por TrainingSession (CTRL-8, THR-41).

    Thread-safety: uso síncrono; si se usa en async, coordinar externamente.
    """

    def __init__(
        self,
        *,
        max_calls: int = _DEFAULT_MAX_CALLS,
        max_usd: float = _DEFAULT_MAX_USD,
        cost_per_call_usd: float = _DEFAULT_COST_PER_CALL_USD,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls debe ser >= 1")
        if max_usd <= 0:
            raise ValueError("max_usd debe ser > 0")
        self._max_calls = max_calls
        self._max_usd = max_usd
        self._cost_per_call = cost_per_call_usd
        self._sessions: dict[UUID, SessionBudgetState] = {}

    def check_and_increment(
        self,
        *,
        tenant_id: UUID,
        training_session_id: UUID,
    ) -> None:
        """Verifica budget y registra la llamada.

        Levanta LlmBudgetExceeded si se supera el límite antes de incrementar.
        """
        state = self._sessions.setdefault(training_session_id, SessionBudgetState())

        if state.calls_made >= self._max_calls:
            self._emit_exceeded(tenant_id, training_session_id, "calls_limit")
            raise LlmBudgetExceeded(
                tenant_id,
                training_session_id,
                f"calls_limit: {state.calls_made} >= {self._max_calls}",
            )

        projected_usd = state.usd_spent + self._cost_per_call
        if projected_usd > self._max_usd:
            self._emit_exceeded(tenant_id, training_session_id, "usd_limit")
            raise LlmBudgetExceeded(
                tenant_id,
                training_session_id,
                f"usd_limit: projected ${projected_usd:.4f} > max ${self._max_usd:.4f}",
            )

        state.calls_made += 1
        state.usd_spent += self._cost_per_call

        logger.info(
            "decision_rule_inferencer_calls_total",
            extra={
                "tenant_id": str(tenant_id),
                "training_session_id": str(training_session_id),
                "calls_made": state.calls_made,
                "usd_spent": round(state.usd_spent, 4),
            },
        )

    def calls_made(self, training_session_id: UUID) -> int:
        return self._sessions.get(training_session_id, SessionBudgetState()).calls_made

    def usd_spent(self, training_session_id: UUID) -> float:
        return self._sessions.get(training_session_id, SessionBudgetState()).usd_spent

    def _emit_exceeded(
        self,
        tenant_id: UUID,
        training_session_id: UUID,
        reason: str,
    ) -> None:
        logger.warning(
            "llm_budget_exceeded",
            extra={
                "tenant_id": str(tenant_id),
                "training_session_id": str(training_session_id),
                "reason": reason,
                "max_calls": self._max_calls,
                "max_usd": self._max_usd,
            },
        )
