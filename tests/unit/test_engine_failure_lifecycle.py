"""Real queue/mirror/stream regressions for one chat turn across bounded retries."""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from hermes.domain.reasoning_failure import NativeTurnFailedError
from hermes.tasks.control_plane.application.stream_broker import StreamBroker
from hermes.tasks.control_plane.domain.ports import StreamChunkKind, TaskStreamChunk
from hermes.tasks.control_plane.infrastructure.chunk_sink import ChunkSinkAdapter
from hermes.tasks.domain import work_item
from hermes.tasks.domain.ports import TaskStatus
from hermes.tasks.infrastructure.sqlite_conversation_repo import SQLiteConversationRepository
from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue
from hermes.testing import FakeReasoningEngine, scripted_response
from tests.tasks.test_agent_loop import _chat_item, _make_chat_orchestrator

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class FailingEngine:
    async def run_cycle(self, _context):
        raise RuntimeError("private synthetic provider detail")


async def setup_turn(tmp_path, engine, *, max_attempts=3):
    queue = SqliteWorkQueue(db_path=tmp_path / "tasks.db")
    repo = SQLiteConversationRepository(db_path=tmp_path / "conversations.db")
    conv = uuid4()
    repo.create_or_touch(conversation_id=conv, first_user_message="Hello")
    raw = _chat_item()
    item = await queue.enqueue(
        replace(
            raw,
            max_attempts=max_attempts,
            payload={**raw.payload, "conversation_id": str(conv)},
        )
    )
    repo.append_message(conversation_id=conv, role="user", content="Hello", task_id=item.id)
    orch, _, sink = _make_chat_orchestrator(engine=engine, queue=queue)
    orch._conversation_repo = repo
    orch._notification_store = MagicMock()
    orch._memory_extraction_enabled = False
    return orch, queue, repo, conv, item, sink


async def attempt(orch, queue):
    claimed = await queue.claim_next()
    assert claimed is not None
    await orch._process(claimed)
    return await queue._load_item(str(claimed.id))


def assistant_rows(repo, conv):
    return [
        row for row in repo.get_detail(conversation_id=conv).messages if row.role == "assistant"
    ]


async def test_three_failed_attempts_produce_one_terminal_row_notification_and_done(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(work_item, "_BACKOFF_BASE_SECONDS", 0)
    orch, queue, repo, conv, item, sink = await setup_turn(tmp_path, FailingEngine())
    original_close = sink.close

    async def close_after_commit(**kwargs):
        assert (await queue._load_item(str(item.id))).status is TaskStatus.FAILED
        assert len(assistant_rows(repo, conv)) == 1
        assert assistant_rows(repo, conv)[0].status == "failed"
        await original_close(**kwargs)

    sink.close = close_after_commit
    for count in (1, 2):
        updated = await attempt(orch, queue)
        assert updated.status is TaskStatus.PENDING
        assert updated.attempts == count
        assert assistant_rows(repo, conv) == []
        assert sink.closed == []
        assert sink.statuses[-1]["status"] == "pending"
        orch._notification_store.add.assert_not_called()
    updated = await attempt(orch, queue)
    assert updated.status is TaskStatus.FAILED
    assert updated.attempts == 3
    rows = assistant_rows(repo, conv)
    assert len(rows) == 1
    assert rows[0].task_id == str(item.id)
    assert rows[0].status == "failed"
    assert str(item.id) in rows[0].content
    assert "private synthetic" not in rows[0].content
    assert len(sink.closed) == 1 and sink.closed[0]["outcome"] == "failed"
    orch._notification_store.add.assert_called_once()
    assert await queue.claim_next() is None
    reopened = SQLiteConversationRepository(db_path=tmp_path / "conversations.db")
    assert len(assistant_rows(reopened, conv)) == 1


async def test_retry_then_success_has_no_error_row_or_premature_terminal_replay(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(work_item, "_BACKOFF_BASE_SECONDS", 0)

    class RecoveringEngine:
        calls = 0

        async def run_cycle(self, _context):
            self.calls += 1
            if self.calls == 1:
                raise NativeTurnFailedError("unavailable", retryable=True)
            return await FakeReasoningEngine(
                scripted=[
                    scripted_response(narrative="Recovered answer", proposals=()),
                ]
            ).run_cycle(_context)

    orch, queue, repo, conv, item, _ = await setup_turn(tmp_path, RecoveringEngine())
    broker = StreamBroker()
    orch._chunk_sink = ChunkSinkAdapter(broker=broker)
    assert (await attempt(orch, queue)).status is TaskStatus.PENDING
    assert item.id not in broker._terminal_done
    assert assistant_rows(repo, conv) == []
    assert (await attempt(orch, queue)).status is TaskStatus.COMPLETED
    frames = [frame async for frame in broker.subscribe(task_id=item.id)]
    terminals = [frame for frame in frames if frame.kind.value in {"done", "error"}]
    assert len(terminals) == 1
    assert terminals[0].payload["outcome"] == "completed"
    assert any(frame.payload.get("status") == "pending" for frame in frames)
    rows = assistant_rows(repo, conv)
    assert [(row.content, row.status) for row in rows] == [("Recovered answer", "complete")]
    orch._notification_store.add.assert_called_once()
    assert orch._notification_store.add.call_args.kwargs["status"] == "ok"


async def test_nonretryable_failure_replaces_streaming_partial_without_extra_message(tmp_path):
    class PartialEngine:
        async def run_cycle(self, context):
            for _ in range(12):
                await context.metadata["chunk_sink"].emit(
                    task_id=context.metadata["task_id_for_stream"],
                    chunk=TaskStreamChunk(kind=StreamChunkKind.DELTA, delta="partial "),
                )
            raise NativeTurnFailedError("incomplete", retryable=False)

    orch, queue, repo, conv, _, sink = await setup_turn(tmp_path, PartialEngine())
    updated = await attempt(orch, queue)
    assert updated.status is TaskStatus.FAILED and updated.attempts == 1
    rows = assistant_rows(repo, conv)
    assert len(rows) == 1 and rows[0].status == "failed"
    assert "partial" not in rows[0].content
    assert "sin confirmar" in rows[0].content
    assert len(sink.closed) == 1


async def test_stream_close_failure_cannot_undo_terminal_queue_or_mirror(tmp_path):
    orch, queue, repo, conv, _, sink = await setup_turn(tmp_path, FailingEngine(), max_attempts=1)
    sink.close = AsyncMock(side_effect=RuntimeError("stream unavailable"))
    updated = await attempt(orch, queue)
    assert updated.status is TaskStatus.FAILED
    assert assistant_rows(repo, conv)[0].status == "failed"
    sink.close.assert_awaited_once()


async def test_mirror_failure_still_closes_after_terminal_commit(tmp_path):
    orch, queue, repo, _, _, sink = await setup_turn(tmp_path, FailingEngine(), max_attempts=1)
    repo.upsert_assistant_message = MagicMock(side_effect=RuntimeError("mirror unavailable"))
    assert (await attempt(orch, queue)).status is TaskStatus.FAILED
    assert len(sink.closed) == 1 and sink.closed[0]["outcome"] == "failed"


async def test_queue_failure_does_not_publish_or_persist_uncommitted_terminal_state(tmp_path):
    orch, queue, repo, conv, _, sink = await setup_turn(tmp_path, FailingEngine(), max_attempts=1)
    queue.mark_failed = AsyncMock(side_effect=RuntimeError("claim changed"))
    with pytest.raises(RuntimeError, match="claim changed"):
        await attempt(orch, queue)
    assert assistant_rows(repo, conv) == []
    assert sink.closed == []
    orch._notification_store.add.assert_not_called()


async def test_retry_status_delivery_failure_preserves_durable_pending(tmp_path):
    orch, queue, repo, conv, _, sink = await setup_turn(tmp_path, FailingEngine())
    original_status = sink.emit_status

    async def status(**kwargs):
        if kwargs["status"] == "pending":
            raise RuntimeError("stream unavailable")
        await original_status(**kwargs)

    sink.emit_status = status
    assert (await attempt(orch, queue)).status is TaskStatus.PENDING
    assert assistant_rows(repo, conv) == [] and sink.closed == []
