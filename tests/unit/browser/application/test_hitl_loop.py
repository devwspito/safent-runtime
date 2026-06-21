"""T701 — Tests del HitlLoop (US5/AC1-AC5).

AC1: confidence<threshold → no ejecuta step + send_frame invocado + request_intervention emite.
AC2: Future resolves con acción del operador → flow ejecuta esa acción.
AC3: intervención materializa Selector(author=OPERATOR_INTERVENTION) + DecisionRule opcional.
AC4: timeout sin respuesta → degraded; step nunca ejecutado autónomamente.
AC5: risk=HIGH + confidence alta → exige hitl_approval_token; sin token → HitlApprovalRequired.

Constitution II inquebrantable: gate HIGH != pausa por confidence.
Constitution IV: timeout → degraded, no auto-execute.
Constitution V: sin Chromium/Postgres/WS real.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.browser.application.hitl_loop import HitlContext, HitlLoop
from hermes.browser.application.session import HitlApprovalRequired
from hermes.browser.domain.ports.intervention_store import OperatorIntervention
from hermes.browser.domain.selector import SelectorAuthor
from hermes.browser.domain.step import StepRisk
from hermes.browser.infrastructure.in_memory_selector_registry import (
    InMemorySelectorRegistry,
)
from hermes.browser.infrastructure.signed_selector_registry import SignedSelectorRegistry
from hermes.browser.testing.in_memory_intervention_store import InMemoryInterventionStore
from hermes.browser.testing.in_memory_live_view_channel import (
    InMemoryLiveViewChannel,
    OperatorInterventionRequest,
)

_OPERATOR = uuid4()
_TENANT = uuid4()
_SESSION = uuid4()
_SIGNING_KEY = b"test-signing-key-32bytes-padding!"
_VALID_TOKEN = "tok_valid_test"


def _make_ctx(
    *,
    risk: StepRisk = StepRisk.MEDIUM,
    reason: str = "confidence_low",
    confidence: float = 0.6,
) -> HitlContext:
    return HitlContext(
        step_id=uuid4(),
        session_id=_SESSION,
        tenant_id=_TENANT,
        site_id="stub",
        flow_id="flow1",
        risk=risk,
        reason=reason,
        confidence=confidence,
        dom_pre_uri="s3://bucket/dom_pre.html",
        screenshot_pre_uri="s3://bucket/screenshot_pre.png",
        intent_desc="click submit button",
    )


def _make_registry() -> SignedSelectorRegistry:
    in_memory = InMemorySelectorRegistry()
    return SignedSelectorRegistry(store=in_memory, signing_key=_SIGNING_KEY)


# ---------------------------------------------------------------------------
# AC1: confidence<threshold → send_frame invocado + request_intervention emitido
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac1_low_confidence_pauses_and_sends_frame() -> None:
    """US5/AC1: confidence<threshold no ejecuta step pero notifica al operador."""
    channel = InMemoryLiveViewChannel()
    store = InMemoryInterventionStore()
    loop = HitlLoop(live_view_channel=channel, intervention_store=store)

    ctx = _make_ctx(risk=StepRisk.MEDIUM, confidence=0.6)

    outcome = await loop.pause_and_request(
        ctx,
        operator_id=_OPERATOR,
        subscription_token=_VALID_TOKEN,
        timeout_s=5.0,
    )

    # send_frame fue invocado
    assert len(channel.frames_sent) >= 1
    # request_intervention fue emitido
    assert len(channel.intervention_requests) == 1
    # El outcome no es degraded (operador respondió via default)
    assert not outcome.degraded
    assert outcome.action_to_execute is not None


# ---------------------------------------------------------------------------
# AC2: Future resolves con acción del operador → flow ejecuta esa acción
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac2_resolved_future_returns_operator_action() -> None:
    """US5/AC2: cuando el operador responde, HitlOutcome contiene la acción."""
    operator_action = {"action": "click", "selector": "#submit-btn", "confirmed": True}

    channel = InMemoryLiveViewChannel()
    channel.inject_response(
        OperatorInterventionRequest(
            operator_id=_OPERATOR,
            session_id=_SESSION,
            action="click",
            payload=operator_action,
        )
    )

    store = InMemoryInterventionStore()
    loop = HitlLoop(live_view_channel=channel, intervention_store=store)
    ctx = _make_ctx()

    outcome = await loop.pause_and_request(
        ctx,
        operator_id=_OPERATOR,
        subscription_token=_VALID_TOKEN,
        timeout_s=5.0,
    )

    assert not outcome.degraded
    # action_to_execute proviene del operador
    assert outcome.action_to_execute is not None
    # La intervención fue persistida
    interventions = await store.interventions_for_session(_SESSION)
    assert len(interventions) == 1
    assert isinstance(interventions[0], OperatorIntervention)


# ---------------------------------------------------------------------------
# AC3: intervención materializa Selector(author=OPERATOR_INTERVENTION) + DecisionRule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac3_intervention_materializes_selector_and_rule() -> None:
    """US5/AC3: la intervención puede producir Selector firmado + DecisionRule."""
    selector_proposal = {
        "strategy": "css",
        "value": "#btn-confirm",
        "intent_desc": "confirm button found by operator",
    }
    rule_proposal = {
        "pattern": {"dom_contains": "Confirmar presentación"},
        "action": {"selector": "#btn-confirm", "action": "click"},
    }

    channel = InMemoryLiveViewChannel()
    channel.inject_response(
        OperatorInterventionRequest(
            operator_id=_OPERATOR,
            session_id=_SESSION,
            action="click",
            payload={
                "action": "click",
                "selector_proposal": selector_proposal,
                "rule_proposal": rule_proposal,
            },
        )
    )

    store = InMemoryInterventionStore()
    registry = _make_registry()
    loop = HitlLoop(
        live_view_channel=channel,
        intervention_store=store,
        selector_registry=registry,
    )
    ctx = _make_ctx()

    outcome = await loop.pause_and_request(
        ctx,
        operator_id=_OPERATOR,
        subscription_token=_VALID_TOKEN,
        timeout_s=5.0,
    )

    assert not outcome.degraded
    # Selector con author=OPERATOR_INTERVENTION
    assert outcome.new_selector is not None
    assert outcome.new_selector.author == SelectorAuthor.OPERATOR_INTERVENTION
    # DecisionRule persistida
    assert outcome.new_rule is not None
    rules = await store.rules_for(
        site_id=ctx.site_id,
        flow_id=ctx.flow_id,
        step_id=str(ctx.step_id),
    )
    assert len(rules) == 1


# ---------------------------------------------------------------------------
# AC4: timeout → degraded; step nunca ejecutado autónomamente
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac4_timeout_results_in_degraded_outcome() -> None:
    """US5/AC4: si nadie responde en timeout_s, el flow degrada.

    El step nunca es ejecutado autónomamente (Constitution IV).
    """
    channel = InMemoryLiveViewChannel(hold_intervention=True)
    store = InMemoryInterventionStore()
    loop = HitlLoop(live_view_channel=channel, intervention_store=store)
    ctx = _make_ctx()

    outcome = await loop.pause_and_request(
        ctx,
        operator_id=_OPERATOR,
        subscription_token=_VALID_TOKEN,
        timeout_s=0.05,  # tiny timeout para test rápido
    )

    assert outcome.degraded is True
    assert "timeout" in outcome.degraded_reason.lower()
    # Nada fue persistido en el store — step no se ejecutó
    interventions = await store.interventions_for_session(_SESSION)
    assert len(interventions) == 0


# ---------------------------------------------------------------------------
# AC5: risk=HIGH + confidence alta → SIEMPRE exige hitl_approval_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac5_high_risk_always_requires_hitl_token_even_with_high_confidence() -> None:
    """US5/AC5: Constitution II inquebrantable.

    risk=HIGH con confidence=0.99 → HitlApprovalRequired si no hay token.
    La pausa HITL profunda NO sustituye el gate de aprobación HIGH.
    """
    channel = InMemoryLiveViewChannel()
    store = InMemoryInterventionStore()
    loop = HitlLoop(live_view_channel=channel, intervention_store=store)

    ctx = _make_ctx(risk=StepRisk.HIGH, confidence=0.99)

    with pytest.raises(HitlApprovalRequired):
        await loop.pause_and_request(
            ctx,
            operator_id=_OPERATOR,
            subscription_token=_VALID_TOKEN,
            timeout_s=5.0,
            hitl_approval_token=None,  # ausente → HitlApprovalRequired
        )

    # send_frame y request_intervention NO fueron llamados
    assert len(channel.frames_sent) == 0
    assert len(channel.intervention_requests) == 0
