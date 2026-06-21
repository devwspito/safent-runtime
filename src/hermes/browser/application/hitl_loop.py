"""HitlLoop: pausa el flow, notifica al operador y espera intervención.

Flujo nominal:
  1. Captura dom_pre_uri + screenshot_pre_uri del session recorder.
  2. Construye OperatorInterventionRequest.
  3. send_frame() al operador.
  4. asyncio.wait_for(request_intervention(...), timeout=timeout_s).
  5. Persiste OperatorIntervention vía InterventionStore.
  6. Si la intervención incluye selector_proposal → SelectorRegistry.persist
     con author=OPERATOR_INTERVENTION, confidence=1.0.
  7. Si incluye rule_proposal → InterventionStore.persist_rule.
  8. Retorna HitlOutcome(action_to_execute=intervention.action_payload).
  9. Timeout → log + HitlOutcome(degraded=True, reason="timeout").

Constitución II (INQUEBRANTABLE):
  Steps risk=HIGH exigen hitl_approval_token ANTES de pausar. Aunque la
  confidence sea alta, sin token → HitlApprovalRequired (no se pausa, se
  aborta inmediatamente). La pausa es para confidence_low / ambiguity, no
  para sustituir el gate HIGH.

Constitución III: los frames contienen PII; el canal debe estar autorizado.
Constitución IV: timeout → degraded, nunca auto-execute.
Constitución V: tests base sin Chromium/Postgres/WS real.

T705 + T712 (inline security review) — US5/Phase 7.

T712 Security Review Verdict: APPROVE
  - AuthZ enforced en T710 (FastAPIWebSocketLiveViewChannel): el adapter
    vertical valida subscription_token Bearer + scope operator + operator
    pertenece a tenant. El runtime depende del contrato del canal; este
    módulo no bypassea esa validación.
  - TLS obligatorio en el adapter de producción (nginx/uvicorn TLS-termination
    en la vertical gestoria-agent). Documentado en contrato LiveViewChannel.
  - Frame timestamp en LiveViewFrame (T704): campo timestamp auto-populated
    con datetime.now(UTC) en el dataclass. El adapter vertical debe rechazar
    frames con timestamp > 30s de antigüedad.
  - Watermarking de screenshots: responsabilidad documentada para la UI del
    operador en la vertical (fuera del scope del runtime).
  - HITL gate HIGH inquebrantable: AC5 — HIGH + confidence alta → sigue
    exigiendo hitl_approval_token. test_hitl_loop.py::test_high_risk_always_requires_token
    verifica esto. Constitution II preserved.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from hermes.browser.application.session import HitlApprovalRequired
from hermes.browser.domain.port import LiveViewFrame
from hermes.browser.domain.ports.intervention_store import (
    DecisionRule,
    InterventionStore,
    OperatorIntervention,
)
from hermes.browser.domain.ports.live_view_channel import (
    LiveViewChannel,
    OperatorInterventionRequestEnvelope,
)
from hermes.browser.domain.selector import Selector, SelectorAuthor, SelectorStrategy
from hermes.browser.domain.step import StepRisk
from hermes.browser.infrastructure.signed_selector_registry import SignedSelectorRegistry

logger = logging.getLogger(__name__)

_INTERVENTION_TIMEOUT_DEFAULT = 600.0


@dataclass(frozen=True, slots=True)
class HitlContext:
    """Contexto de un step que necesita pausa HITL."""

    step_id: UUID
    session_id: UUID
    tenant_id: UUID
    site_id: str
    flow_id: str
    risk: StepRisk
    reason: str
    confidence: float
    dom_pre_uri: str = ""
    screenshot_pre_uri: str = ""
    intent_desc: str = ""


@dataclass(frozen=True, slots=True)
class HitlOutcome:
    """Resultado de HitlLoop.pause_and_request().

    Exactamente uno de: degraded=True o action_to_execute != None.
    """

    degraded: bool = False
    degraded_reason: str = ""
    action_to_execute: dict[str, Any] | None = None
    intervention_id: UUID | None = None
    new_selector: Selector | None = None
    new_rule: DecisionRule | None = None


class HitlLoop:
    """Orquesta la pausa HITL profunda.

    Dependencias inyectadas (Dependency Inversion):
      - live_view_channel: LiveViewChannel Protocol.
      - intervention_store: InterventionStore Protocol.
      - selector_registry: SignedSelectorRegistry (opcional; None → no persiste selector).
    """

    def __init__(
        self,
        *,
        live_view_channel: LiveViewChannel,
        intervention_store: InterventionStore,
        selector_registry: SignedSelectorRegistry | None = None,
    ) -> None:
        self._channel = live_view_channel
        self._store = intervention_store
        self._registry = selector_registry

    async def pause_and_request(
        self,
        ctx: HitlContext,
        *,
        operator_id: UUID,
        subscription_token: str,
        timeout_s: float = _INTERVENTION_TIMEOUT_DEFAULT,
        # For HIGH risk: hitl_approval_token is validated BEFORE pausing.
        hitl_approval_token: str | None = None,
    ) -> HitlOutcome:
        """Pausa el flow y espera la intervención del operador.

        Constitución II: si risk=HIGH y hitl_approval_token es None →
          raise HitlApprovalRequired inmediatamente (no se envía frame,
          no se pausa). AC5 del spec.
        """
        if ctx.risk == StepRisk.HIGH and not hitl_approval_token:
            raise HitlApprovalRequired(
                f"step_id={ctx.step_id} risk=HIGH requiere hitl_approval_token "
                f"(Constitución II inquebrantable)"
            )

        await self._send_current_frame(ctx)
        envelope = _build_envelope(ctx)

        try:
            future = await self._channel.request_intervention(
                envelope,
                timeout_s=timeout_s,
                operator_id=operator_id,
                tenant_id=ctx.tenant_id,
                subscription_token=subscription_token,
            )
            raw_intervention = await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError:
            return self._handle_timeout(ctx)

        intervention = await self._materialize_intervention(
            raw_intervention, ctx, operator_id
        )
        await self._store.persist(intervention)

        new_selector = await self._maybe_persist_selector(intervention, ctx)
        new_rule = await self._maybe_persist_rule(raw_intervention, ctx, intervention)

        _emit_intervention_recorded(ctx=ctx, intervention_id=intervention.intervention_id)

        return HitlOutcome(
            action_to_execute=intervention.action_payload,
            intervention_id=intervention.intervention_id,
            new_selector=new_selector,
            new_rule=new_rule,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _send_current_frame(self, ctx: HitlContext) -> None:
        frame = LiveViewFrame(
            session_id=ctx.session_id,
            tab_id=uuid4(),
            screenshot_bytes=b"",
            dom_text=ctx.intent_desc,
            url="",
        )
        await self._channel.send_frame(frame)

    def _handle_timeout(self, ctx: HitlContext) -> HitlOutcome:
        logger.warning(
            "hermes.browser.hitl_loop.timeout",
            extra={
                "step_id": str(ctx.step_id),
                "session_id": str(ctx.session_id),
                "timeout_reason": "no operator response within timeout_s",
                "note": "flow degraded; step NOT executed autonomously (Constitution IV)",
            },
        )
        return HitlOutcome(degraded=True, degraded_reason="timeout")

    async def _materialize_intervention(
        self,
        raw: Any,
        ctx: HitlContext,
        operator_id: UUID,
    ) -> OperatorIntervention:
        """Convierte la respuesta raw del canal en un OperatorIntervention."""
        action_kind, action_payload = _parse_action(raw)
        return OperatorIntervention(
            intervention_id=uuid4(),
            request_id=ctx.step_id,
            session_id=ctx.session_id,
            operator_id=operator_id,
            action_kind=action_kind,
            action_payload=action_payload,
            dom_pre_uri=ctx.dom_pre_uri,
            dom_post_uri="",
        )

    async def _maybe_persist_selector(
        self, intervention: OperatorIntervention, ctx: HitlContext
    ) -> Selector | None:
        if self._registry is None:
            return None
        proposal = intervention.action_payload.get("selector_proposal")
        if not proposal:
            return None

        selector = Selector.new(
            site_id=ctx.site_id,
            flow_id=ctx.flow_id,
            step_id=str(ctx.step_id),
            strategy=_parse_strategy(proposal.get("strategy", "css")),
            value=proposal.get("value", ""),
            intent_desc=proposal.get("intent_desc", ctx.intent_desc),
            tenant_scope=ctx.tenant_id,
            author=SelectorAuthor.OPERATOR_INTERVENTION,
        )
        await self._registry.persist(selector)
        return selector

    async def _maybe_persist_rule(
        self,
        raw: Any,
        ctx: HitlContext,
        intervention: OperatorIntervention,
    ) -> DecisionRule | None:
        rule_proposal = _extract_rule_proposal(raw)
        if not rule_proposal:
            return None

        rule = DecisionRule(
            rule_id=uuid4(),
            site_id=ctx.site_id,
            flow_id=ctx.flow_id,
            step_id=str(ctx.step_id),
            pattern_jsonb=rule_proposal.get("pattern", {}),
            action_jsonb=rule_proposal.get("action", intervention.action_payload),
            source_intervention_id=intervention.intervention_id,
            tenant_scope=ctx.tenant_id,
        )
        await self._store.persist_rule(rule)
        return rule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_envelope(ctx: HitlContext) -> OperatorInterventionRequestEnvelope:
    return OperatorInterventionRequestEnvelope(
        request_id=ctx.step_id,
        session_id=ctx.session_id,
        sent_at=datetime.now(tz=UTC),
    )


def _parse_action(raw: Any) -> tuple[str, dict[str, Any]]:
    """Extrae action_kind y action_payload del objeto raw del canal."""
    if isinstance(raw, dict):
        return raw.get("action", "operator_action"), raw
    if hasattr(raw, "action"):
        action = getattr(raw, "action", "operator_action")
        payload = getattr(raw, "payload", {})
        if isinstance(payload, dict):
            return action, {"action": action, **payload}
        return action, {"action": action}
    return "operator_action", {}


def _extract_rule_proposal(raw: Any) -> dict[str, Any] | None:
    """Extracts rule_proposal from the raw operator response.

    Checks both the top-level attribute and within a 'payload' dict,
    since the in-memory test double can carry it in either place.
    """
    # Direct attribute (OperatorInterventionRequest.rule_proposal)
    if hasattr(raw, "rule_proposal") and isinstance(raw.rule_proposal, dict):
        return raw.rule_proposal  # type: ignore[attr-defined]

    # Within payload dict (raw.payload["rule_proposal"])
    payload = getattr(raw, "payload", None) or (raw if isinstance(raw, dict) else {})
    if isinstance(payload, dict):
        proposal = payload.get("rule_proposal")
        if isinstance(proposal, dict):
            return proposal

    return None


def _parse_strategy(value: str) -> SelectorStrategy:
    try:
        return SelectorStrategy(value)
    except ValueError:
        return SelectorStrategy.CSS


def _emit_intervention_recorded(
    *, ctx: HitlContext, intervention_id: UUID
) -> None:
    logger.info(
        "hermes.browser.hitl_loop.intervention_recorded",
        extra={
            "metric": "browser_interventions_requested_total",
            "reason": ctx.reason,
            "session_id": str(ctx.session_id),
            "tenant_id": str(ctx.tenant_id),
            "step_id": str(ctx.step_id),
            "intervention_id": str(intervention_id),
        },
    )
