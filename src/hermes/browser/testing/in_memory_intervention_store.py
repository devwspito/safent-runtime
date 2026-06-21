"""InMemoryInterventionStore: test double del InterventionStore.

Dict-based. No cifrado. Sin Postgres. Constitución V: usado en tests base.

T707 — US5/Phase 7.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from hermes.browser.domain.ports.intervention_store import (
    DecisionRule,
    InterventionStore,
    OperatorIntervention,
)


class InMemoryInterventionStore:
    """InterventionStore in-memory para tests.

    Implementa el Protocol InterventionStore sin dependencias externas.
    """

    def __init__(self) -> None:
        self._interventions: dict[UUID, OperatorIntervention] = {}
        self._rules: dict[UUID, DecisionRule] = {}

    async def persist(self, intervention: OperatorIntervention) -> None:
        """Idempotente por intervention_id."""
        self._interventions[intervention.intervention_id] = intervention

    async def persist_rule(self, rule: DecisionRule) -> None:
        """Supersede regla previa activa con mismo (site_id, flow_id, step_id, pattern)."""
        for existing in list(self._rules.values()):
            if (
                existing.is_active
                and existing.site_id == rule.site_id
                and existing.flow_id == rule.flow_id
                and existing.step_id == rule.step_id
                and existing.pattern_jsonb == rule.pattern_jsonb
                and existing.rule_id != rule.rule_id
            ):
                self._rules[existing.rule_id] = DecisionRule(
                    rule_id=existing.rule_id,
                    site_id=existing.site_id,
                    flow_id=existing.flow_id,
                    step_id=existing.step_id,
                    pattern_jsonb=existing.pattern_jsonb,
                    action_jsonb=existing.action_jsonb,
                    source_intervention_id=existing.source_intervention_id,
                    tenant_scope=existing.tenant_scope,
                    created_at=existing.created_at,
                    deprecated_at=datetime.now(tz=UTC),
                    deprecation_reason="superseded_by_newer_rule",
                )
        self._rules[rule.rule_id] = rule

    async def rules_for(
        self,
        *,
        site_id: str,
        flow_id: str,
        step_id: str | None = None,
        tenant_scope: UUID | None = None,
    ) -> Sequence[DecisionRule]:
        """Reglas activas para la tripleta, tenant antes que global."""
        matches = [
            r for r in self._rules.values()
            if r.is_active
            and r.site_id == site_id
            and r.flow_id == flow_id
            and (step_id is None or r.step_id == step_id)
            and (tenant_scope is None or r.tenant_scope in {None, tenant_scope})
        ]
        return sorted(
            matches,
            key=lambda r: (0 if r.tenant_scope is not None else 1),
        )

    async def interventions_for_session(
        self, session_id: UUID
    ) -> Sequence[OperatorIntervention]:
        return [
            i for i in self._interventions.values()
            if i.session_id == session_id
        ]

    async def mark_rule_deprecated(
        self, rule_id: UUID, *, reason: str = ""
    ) -> None:
        """Idempotente."""
        rule = self._rules.get(rule_id)
        if rule is None or not rule.is_active:
            return
        self._rules[rule_id] = DecisionRule(
            rule_id=rule.rule_id,
            site_id=rule.site_id,
            flow_id=rule.flow_id,
            step_id=rule.step_id,
            pattern_jsonb=rule.pattern_jsonb,
            action_jsonb=rule.action_jsonb,
            source_intervention_id=rule.source_intervention_id,
            tenant_scope=rule.tenant_scope,
            created_at=rule.created_at,
            deprecated_at=datetime.now(tz=UTC),
            deprecation_reason=reason,
        )


# Verify Protocol compliance at import time (development safety net).
_: InterventionStore = InMemoryInterventionStore()  # type: ignore[assignment]
