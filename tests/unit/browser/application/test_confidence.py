"""T702 — Tests del módulo confidence (US5/Phase 7).

Test 1: provider con logprobs (mock) → confidence extraído correctamente.
Test 2: provider sin logprobs + secondary_llm_call → invoca evaluador secundario.
Test 3: provider sin logprobs + sin secondary → confidence=1.0 + log warning emitido.

Constitution V: sin Chromium/Postgres/red.
"""

from __future__ import annotations

import logging
import math

import pytest

from hermes.browser.application.confidence import (
    ConfidenceConfig,
    ConfidenceEvaluator,
)

# ---------------------------------------------------------------------------
# Test 1: logprobs disponibles → confidence calculado desde logprobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_from_logprobs() -> None:
    """Provider con logprobs → confidence = exp(min(logprobs)) clampado [0,1]."""
    # Simula respuesta LiteLLM con logprobs estructurados.
    logprob_value = -0.5  # exp(-0.5) ≈ 0.607
    mock_response = {
        "choices": [
            {
                "message": {"content": "click #btn"},
                "logprobs": {
                    "content": [
                        {"token": "click", "logprob": logprob_value},
                        {"token": " #btn", "logprob": -0.2},
                    ]
                },
            }
        ]
    }

    evaluator = ConfidenceEvaluator(config=ConfidenceConfig(threshold=0.75))
    confidence = await evaluator.evaluate(llm_response=mock_response, prompt="click submit")

    # min logprob = -0.5 → exp(-0.5) ≈ 0.607
    expected = math.exp(-0.5)
    assert abs(confidence - expected) < 0.01
    assert 0.0 <= confidence <= 1.0


# ---------------------------------------------------------------------------
# Test 2: sin logprobs + secondary_llm_call → evaluador secundario invocado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_secondary_evaluator_invoked_when_no_logprobs() -> None:
    """Sin logprobs → secondary_llm_call es invocado con el prompt de evaluación."""
    calls: list[str] = []

    async def mock_secondary(prompt: str) -> str:
        calls.append(prompt)
        return "0.85"

    mock_response = {"choices": [{"message": {"content": "fill form"}, "logprobs": None}]}

    config = ConfidenceConfig(threshold=0.75, secondary_evaluator_model="gpt-4o-mini")
    evaluator = ConfidenceEvaluator(config=config, secondary_llm_call=mock_secondary)

    confidence = await evaluator.evaluate(
        llm_response=mock_response, prompt="fill the reference field"
    )

    assert len(calls) == 1
    assert "fill the reference field" in calls[0]
    assert abs(confidence - 0.85) < 0.01


# ---------------------------------------------------------------------------
# Test 3: sin logprobs + sin secondary → confidence=1.0 + log warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_assumes_high_when_no_logprobs_and_no_secondary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sin logprobs y sin secondary_llm_call → confidence=1.0 + warning log."""
    mock_response = {"choices": [{"message": {"content": "submit"}, "logprobs": None}]}

    config = ConfidenceConfig(threshold=0.75, secondary_evaluator_model=None)
    evaluator = ConfidenceEvaluator(config=config, secondary_llm_call=None)

    with caplog.at_level(logging.WARNING, logger="hermes.browser.confidence"):
        confidence = await evaluator.evaluate(
            llm_response=mock_response, prompt="submit form"
        )

    assert confidence == 1.0
    assert any(
        "no_logprobs_no_secondary" in record.message
        or "logprobs" in record.message.lower()
        for record in caplog.records
    )
