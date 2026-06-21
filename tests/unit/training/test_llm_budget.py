"""Tests del LLM Budget per TrainingSession (T098, BLOCKER CTRL-8)."""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest

from hermes.training.application.llm_budget import (
    BUDGET_EXCEEDED_REASON,
    LlmBudgetCounter,
    LlmBudgetExceeded,
)

pytestmark = pytest.mark.unit


class TestBudgetWithinLimit:
    def test_calls_within_limit_do_not_raise(self) -> None:
        counter = LlmBudgetCounter(max_calls=5, max_usd=1.0, cost_per_call_usd=0.01)
        tenant = uuid4()
        session = uuid4()
        for _ in range(5):
            counter.check_and_increment(tenant_id=tenant, training_session_id=session)
        assert counter.calls_made(session) == 5

    def test_calls_counter_increments_correctly(self) -> None:
        counter = LlmBudgetCounter(max_calls=10)
        session = uuid4()
        tenant = uuid4()
        counter.check_and_increment(tenant_id=tenant, training_session_id=session)
        counter.check_and_increment(tenant_id=tenant, training_session_id=session)
        assert counter.calls_made(session) == 2

    def test_usd_spent_accumulates(self) -> None:
        counter = LlmBudgetCounter(max_calls=100, max_usd=1.0, cost_per_call_usd=0.10)
        session = uuid4()
        tenant = uuid4()
        for _ in range(3):
            counter.check_and_increment(tenant_id=tenant, training_session_id=session)
        assert abs(counter.usd_spent(session) - 0.30) < 0.001


class TestBudgetExceeded:
    def test_call_limit_exceeded_raises(self) -> None:
        counter = LlmBudgetCounter(max_calls=3, max_usd=10.0, cost_per_call_usd=0.01)
        tenant = uuid4()
        session = uuid4()
        for _ in range(3):
            counter.check_and_increment(tenant_id=tenant, training_session_id=session)
        with pytest.raises(LlmBudgetExceeded) as exc_info:
            counter.check_and_increment(tenant_id=tenant, training_session_id=session)
        assert "calls_limit" in exc_info.value.reason
        assert exc_info.value.training_session_id == session
        assert exc_info.value.tenant_id == tenant

    def test_usd_limit_exceeded_raises(self) -> None:
        counter = LlmBudgetCounter(max_calls=100, max_usd=0.05, cost_per_call_usd=0.03)
        tenant = uuid4()
        session = uuid4()
        counter.check_and_increment(tenant_id=tenant, training_session_id=session)
        # 0.03 + 0.03 = 0.06 > 0.05
        with pytest.raises(LlmBudgetExceeded) as exc_info:
            counter.check_and_increment(tenant_id=tenant, training_session_id=session)
        assert "usd_limit" in exc_info.value.reason

    def test_exceeded_emits_warning_log(self, caplog: pytest.LogCaptureFixture) -> None:
        counter = LlmBudgetCounter(max_calls=1)
        tenant = uuid4()
        session = uuid4()
        counter.check_and_increment(tenant_id=tenant, training_session_id=session)
        with caplog.at_level(logging.WARNING):
            with pytest.raises(LlmBudgetExceeded):
                counter.check_and_increment(tenant_id=tenant, training_session_id=session)
        assert any("llm_budget_exceeded" in r.getMessage() for r in caplog.records)


class TestBudgetIsolation:
    def test_different_sessions_are_independent(self) -> None:
        counter = LlmBudgetCounter(max_calls=2)
        tenant = uuid4()
        session_a = uuid4()
        session_b = uuid4()
        counter.check_and_increment(tenant_id=tenant, training_session_id=session_a)
        counter.check_and_increment(tenant_id=tenant, training_session_id=session_a)
        # session_b no tiene ninguna llamada → debe poder hacer 2 sin error
        counter.check_and_increment(tenant_id=tenant, training_session_id=session_b)
        counter.check_and_increment(tenant_id=tenant, training_session_id=session_b)
        assert counter.calls_made(session_a) == 2
        assert counter.calls_made(session_b) == 2

    def test_zero_calls_unknown_session_returns_zero(self) -> None:
        counter = LlmBudgetCounter()
        assert counter.calls_made(uuid4()) == 0
        assert counter.usd_spent(uuid4()) == 0.0


class TestBudgetConfig:
    def test_invalid_max_calls_raises(self) -> None:
        with pytest.raises(ValueError):
            LlmBudgetCounter(max_calls=0)

    def test_invalid_max_usd_raises(self) -> None:
        with pytest.raises(ValueError):
            LlmBudgetCounter(max_usd=0.0)
