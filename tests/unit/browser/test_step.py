from __future__ import annotations

from uuid import uuid4

from hermes.browser import Step, StepKind, StepOutcome, StepRisk, StepStatus


def test_step_new_assigns_ids() -> None:
    s = Step.new(
        tenant_id=uuid4(),
        session_id=uuid4(),
        kind=StepKind.NAVIGATE,
        risk=StepRisk.LOW,
        intent_desc="go to AEAT",
        payload={"url": "https://example.com"},
    )
    assert s.step_id is not None
    assert s.step_group_id is not None
    assert s.requires_hitl is False


def test_step_high_requires_hitl() -> None:
    s = Step.new(
        tenant_id=uuid4(),
        session_id=uuid4(),
        kind=StepKind.ACT,
        risk=StepRisk.HIGH,
        intent_desc="submit definitivo",
        payload={"instruction": "click presentar"},
    )
    assert s.requires_hitl is True


def test_outcome_ok_helper() -> None:
    sid = uuid4()
    out = StepOutcome.ok(step_id=sid, duration_ms=42, result={"k": "v"})
    assert out.status == StepStatus.EXECUTED_OK
    assert out.result == {"k": "v"}
    assert out.duration_ms == 42


def test_outcome_failed_helper() -> None:
    sid = uuid4()
    out = StepOutcome.failed(step_id=sid, error="timeout")
    assert out.status == StepStatus.EXECUTED_FAILED
    assert out.error == "timeout"


def test_outcome_rejected_helper() -> None:
    out = StepOutcome.rejected_by_hitl(step_id=uuid4(), reason="user_denied")
    assert out.status == StepStatus.REJECTED
    assert out.error == "user_denied"
