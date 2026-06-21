"""T703 — Tests del budget LLM en BrowserOrchestrator (US5/Phase 7).

Test 1: budget 5 calls → flow gasta exactamente 5 → ok (no degraded).
Test 2: budget 5 calls → flow intenta 6 → budget_exceeded=True (degraded).

Métrica browser_llm_calls_total verificada via caplog structlog.
Constitution V: sin Chromium/Postgres/LLM real.
Threat-model control P2 #7 / D1 superficie 1.
"""

from __future__ import annotations

import logging

import pytest

from hermes.browser.application.orchestrator import BrowserOrchestrator
from hermes.browser.testing.in_memory_replay_store import InMemoryReplayStore

_SIGNING_KEY = b"test-signing-key-32bytes-padding!"
_SESSION = "session-abc-123"
_FLOW = "flow-test-1"


def _make_orchestrator(budget: int) -> BrowserOrchestrator:
    return BrowserOrchestrator(
        replay_store=InMemoryReplayStore(),
        replay_signing_key=_SIGNING_KEY,
        llm_budget_per_flow=budget,
    )


# ---------------------------------------------------------------------------
# Test 1: budget=5, gasta 5 → todas OK (no excede)
# ---------------------------------------------------------------------------


def test_llm_budget_within_limit_does_not_degrade(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """5 llamadas dentro del budget de 5 → count_llm_call devuelve False (ok)."""
    orchestrator = _make_orchestrator(budget=5)

    with caplog.at_level(logging.INFO):
        results = [
            orchestrator.count_llm_call(_SESSION, _FLOW)
            for _ in range(5)
        ]

    # Ninguna de las 5 supera el budget
    assert all(not exceeded for exceeded in results)
    assert orchestrator.llm_calls_made(_SESSION, _FLOW) == 5

    # browser_llm_calls_total fue emitido en el log
    messages = [r.getMessage() for r in caplog.records]
    assert any("browser_llm_calls_total" in m for m in messages)


# ---------------------------------------------------------------------------
# Test 2: budget=5, intenta 6 → la 6ta supera el budget
# ---------------------------------------------------------------------------


def test_llm_budget_exceeded_on_sixth_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """La llamada 6 supera el budget de 5 → count_llm_call devuelve True (degrade)."""
    orchestrator = _make_orchestrator(budget=5)

    with caplog.at_level(logging.WARNING):
        results = [
            orchestrator.count_llm_call(_SESSION, _FLOW)
            for _ in range(6)
        ]

    # Las primeras 5 no exceden; la 6ta sí
    assert all(not results[i] for i in range(5))
    assert results[5] is True  # budget exceeded
    assert orchestrator.llm_calls_made(_SESSION, _FLOW) == 6

    # Se emitió warning de budget_exceeded
    assert any(
        "budget_exceeded" in r.getMessage() or "BUDGET_EXCEEDED" in r.getMessage()
        for r in caplog.records
    )
