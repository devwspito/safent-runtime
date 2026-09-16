"""Hermes 0.21.1 terminal dictionaries + real task queues, no provider network."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from hermes.domain.proposal import ToolCallProposal
from hermes.domain.reasoning_failure import NativeTurnFailedError
from hermes.runtime.native_turn_result import classify_native_result
from hermes.runtime.nous_engine import NousReasoningEngine
from hermes.tasks.domain.ports import TaskStatus
from hermes.tasks.domain.task_cancel_registry import OperationCancelled
from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue
from tests.tasks.test_agent_loop import _chat_item, _make_chat_orchestrator
from tests.unit.test_nous_engine_hermes021_compat import _persona

pytestmark = pytest.mark.unit
SECRET = "fictional-provider-secret-do-not-publish"


def native_result(**kwargs):
    return {
        "completed": True,
        "failed": False,
        "interrupted": False,
        "partial": False,
        "final_response": "OK",
        "turn_exit_reason": "text_response(finish_reason=stop)",
        **kwargs,
    }


@pytest.mark.parametrize("completed", [True, False])
def test_interrupt_wins_over_completed_and_discards_raw_text(completed):
    with pytest.raises(OperationCancelled) as caught:
        classify_native_result(
            native_result(completed=completed, interrupted=True, final_response=SECRET)
        )
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize(
    "flag", ["completed", "failed", "interrupted", "partial", "failure_retryable"]
)
@pytest.mark.parametrize("value", [None, 1, "false", []])
def test_malformed_flag_is_not_truthy_success(flag, value):
    with pytest.raises(NativeTurnFailedError) as caught:
        classify_native_result(native_result(**{flag: value}))
    assert caught.value.code == "invalid_result" and not caught.value.retryable


@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        {},
        {"final_response": "looks successful"},
        {"completed": False},
        {"completed": True, "partial": True},
    ],
)
def test_no_inferred_success_from_missing_or_partial_result(result):
    with pytest.raises(NativeTurnFailedError):
        classify_native_result(result)


@pytest.mark.parametrize(
    "reason", ["auth", "auth_permanent", "billing", "ssl_cert_verification", "model_not_found"]
)
def test_terminal_provider_failure_never_retries_or_exposes_provider_body(reason):
    with pytest.raises(NativeTurnFailedError) as caught:
        classify_native_result(
            native_result(
                failed=True,
                completed=False,
                error=SECRET,
                final_response=SECRET,
                failure_reason=reason,
                failure_retryable=True,
            )
        )
    assert not caught.value.retryable and SECRET not in str(caught.value)


def test_transient_retry_uses_explicit_native_boolean():
    with pytest.raises(NativeTurnFailedError) as caught:
        classify_native_result(
            {
                "completed": False,
                "failed": True,
                "failure_reason": "rate_limit",
                "failure_retryable": True,
            }
        )
    assert caught.value.retryable and caught.value.code == "rate_limit"


def test_error_even_alongside_completed_is_not_success():
    with pytest.raises(NativeTurnFailedError):
        classify_native_result(native_result(error=SECRET))


def test_unknown_failure_reason_is_not_echoed():
    with pytest.raises(NativeTurnFailedError) as caught:
        classify_native_result(native_result(failed=True, failure_reason=SECRET))
    assert caught.value.code == "failed" and SECRET not in str(caught.value)


def test_provider_object_is_not_stringified_into_success():
    with pytest.raises(NativeTurnFailedError):
        classify_native_result(native_result(final_response={"authorization": SECRET}))


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable", [True, False])
async def test_sqlite_failure_cas_rejects_claim_lost_after_snapshot(
    tmp_path, monkeypatch, retryable
):
    from hermes.tasks.infrastructure.sqlite_work_queue import ClaimTokenMismatch

    queue = SqliteWorkQueue(db_path=tmp_path / "q.db")
    item = await queue.enqueue(_chat_item())
    claimed = await queue.claim_next()
    load = queue._load_item

    async def racing_load(task_id):
        snapshot = await load(task_id)
        await queue.mark_cancelled(item.id, claim_token=claimed.claim_token, reason="cancelled")
        return snapshot

    monkeypatch.setattr(queue, "_load_item", racing_load)
    with pytest.raises(ClaimTokenMismatch):
        await queue.mark_failed(
            item.id, claim_token=claimed.claim_token, reason="safe", retryable=retryable
        )
    assert (await load(str(item.id))).status is TaskStatus.CANCELLED


def test_actual_native_success_maps_to_narrative_and_pending_approval_stays_separate():
    engine = NousReasoningEngine(persona=_persona())
    agent = SimpleNamespace(_pending_proposals=[], _read_external_content=False)
    assert engine._map_result_to_output(native_result(), {}, "test", agent).narrative == "OK"
    pending = ToolCallProposal(
        proposal_id=uuid4(),
        tool_name="crm_update",
        tenant_id=uuid4(),
        entity_id="customer",
        entity_type="crm",
        parameters={},
        justification="Human review required",
    )
    agent._pending_proposals = [pending]
    output = engine._map_result_to_output(
        native_result(completed=False, final_response=SECRET), {}, "test", agent
    )
    assert output.narrative == "" and output.tool_call_proposals == (pending,)
    with pytest.raises(NativeTurnFailedError):
        engine._map_result_to_output(
            native_result(failed=True, final_response=SECRET), {}, "test", agent
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("retryable", [True, False])
async def test_queue_retry_decision_preserves_attempts_and_claim_authority(
    tmp_path, kind, retryable
):
    queue = InMemoryWorkQueue() if kind == "memory" else SqliteWorkQueue(db_path=tmp_path / "q.db")
    item = await queue.enqueue(_chat_item())
    claimed = await queue.claim_next()
    with pytest.raises(ValueError):
        await queue.mark_failed(item.id, claim_token=uuid4(), reason="safe", retryable=retryable)
    state = await queue.mark_failed(
        item.id, claim_token=claimed.claim_token, reason="safe", retryable=retryable
    )
    assert state.status is (TaskStatus.PENDING if retryable else TaskStatus.FAILED)
    assert state.attempts == 1 and state.max_attempts > 1
    assert state.claim_token is None
    if kind == "sqlite":
        restarted = SqliteWorkQueue(db_path=tmp_path / "q.db")
        assert (await restarted._load_item(str(item.id))).status is state.status
        assert await restarted.reconcile_stale() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected,retryable",
    [
        ("auth", TaskStatus.FAILED, False),
        ("billing", TaskStatus.FAILED, False),
        ("rate_limit", TaskStatus.PENDING, True),
        ("interrupt", TaskStatus.CANCELLED, False),
    ],
)
async def test_real_orchestrator_never_publishes_failed_sdk_result_as_success(
    tmp_path, caplog, case, expected, retryable
):
    result = native_result(
        completed=False,
        failed=True,
        error=SECRET,
        final_response=SECRET,
        failure_reason=case,
        failure_retryable=retryable,
    )
    if case == "interrupt":
        result = native_result(interrupted=True, final_response=SECRET)

    class NativeResultEngine(NousReasoningEngine):
        async def run_cycle(self, context):  # noqa: ARG002 - engine port
            return self._map_result_to_output(
                result,
                {},
                "test",
                SimpleNamespace(_pending_proposals=[], _read_external_content=False),
            )

    queue = SqliteWorkQueue(db_path=tmp_path / "q.db")
    engine = NativeResultEngine(persona=_persona())
    orch, _, sink = _make_chat_orchestrator(engine=engine, queue=queue)
    repo = MagicMock()
    orch._conversation_repo = repo
    raw = _chat_item()
    item = await queue.enqueue(
        replace(raw, payload={**raw.payload, "conversation_id": str(uuid4())})
    )
    claimed = await queue.claim_next()
    with caplog.at_level("INFO"):
        await orch._process(claimed)
    restarted = SqliteWorkQueue(db_path=tmp_path / "q.db")
    state = await restarted._load_item(str(item.id))
    assert state.status is expected and state.attempts == 1
    assert (
        "hermes.tasks.loop.task_completed" not in caplog.text and "chat_replied" not in caplog.text
    )
    assert sink.emitted == []
    assert all(entry["outcome"] != "completed" for entry in sink.closed)
    assert SECRET not in str(sink.closed) + caplog.text + str(repo.mock_calls)
    assert repo.append_message.called
    assert await restarted.claim_next() is None
