"""InterventionStore: puerto de dominio para persistencia de intervenciones.

Materializa OperatorIntervention + DecisionRule aprendidas.

Constitución II: DecisionRule es contexto al LLM, no autorización. El
  HITL gate HIGH sigue activo aunque exista una DecisionRule para el step.
Constitución IV: fail-closed. persist/persist_rule idempotentes por ID.

T707 — US5/Phase 7.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OperatorIntervention:
    """Acción del operador humano materializada y persistida.

    action_kind: "click" | "fill" | "navigate" | "skip" | "abort"
    action_payload: selector usado, valores, etc.
    dom_pre_uri / dom_post_uri: URIs en el storage del recorder (PII at-rest).

    Constitución III: action_payload puede contener datos del cliente;
      el adapter Postgres cifra el payload con AES-GCM-256 + AAD (T709).
    """

    intervention_id: UUID
    request_id: UUID
    session_id: UUID
    operator_id: UUID
    action_kind: str
    action_payload: dict[str, Any]
    dom_pre_uri: str
    dom_post_uri: str
    notes: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass(frozen=True, slots=True)
class DecisionRule:
    """Patrón aprendido de una intervención.

    Consumible como contexto LLM en sesiones futuras del mismo
    (site_id, flow_id, step_id).

    Constitución II: rule es contexto al LLM, no autorización de eludir HITL.
    """

    rule_id: UUID
    site_id: str
    flow_id: str
    step_id: str
    pattern_jsonb: dict[str, Any]   # "cuando el DOM presenta X"
    action_jsonb: dict[str, Any]    # "hacer Y"
    source_intervention_id: UUID
    tenant_scope: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    deprecated_at: datetime | None = None
    deprecation_reason: str = ""

    @property
    def is_active(self) -> bool:
        return self.deprecated_at is None


@runtime_checkable
class InterventionStore(Protocol):
    """Persistencia de intervenciones + reglas aprendidas."""

    async def persist(self, intervention: OperatorIntervention) -> None:
        """Guarda la intervención. Idempotente por intervention_id."""
        ...

    async def persist_rule(self, rule: DecisionRule) -> None:
        """Guarda una DecisionRule.

        Si existe una activa con misma tripleta (site_id, flow_id, step_id)
        y mismo pattern_jsonb, la nueva supersede (la previa se marca
        deprecated).
        """
        ...

    async def rules_for(
        self,
        *,
        site_id: str,
        flow_id: str,
        step_id: str | None = None,
        tenant_scope: UUID | None = None,
    ) -> Sequence[DecisionRule]:
        """Devuelve reglas activas para la tripleta.

        Combina globales (tenant_scope=None) + las del tenant si se pasa.
        Ordenadas por especificidad (tenant antes que global).
        """
        ...

    async def interventions_for_session(
        self, session_id: UUID
    ) -> Sequence[OperatorIntervention]:
        """Histórico de intervenciones de una sesión (audit)."""
        ...

    async def mark_rule_deprecated(
        self, rule_id: UUID, *, reason: str = ""
    ) -> None:
        """Depreca una regla. Idempotente."""
        ...
