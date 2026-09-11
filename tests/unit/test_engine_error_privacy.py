"""Engine exceptions are untrusted data, not operator-facing diagnostics."""

from dataclasses import replace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from hermes.tasks.domain.ports import TaskStatus
from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue
from tests.tasks.test_agent_loop import _chat_item, _make_chat_orchestrator

pytestmark = pytest.mark.unit
SECRET = "fictional-error-token-must-not-escape"


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile_string", [False, True])
async def test_engine_exception_is_not_echoed_or_stringified(tmp_path, caplog, hostile_string):
    class HostileError(Exception):
        def __str__(self):
            raise AssertionError("An untrusted exception must not be stringified")

    class Engine:
        async def run_cycle(self, _context):
            if hostile_string:
                raise HostileError(SECRET)
            raise RuntimeError(
                f"Authorization: Bearer {SECRET}\nhttps://fixture.invalid/?key={SECRET}"
            )

    queue = SqliteWorkQueue(db_path=tmp_path / "tasks.db")
    orchestrator, _, sink = _make_chat_orchestrator(engine=Engine(), queue=queue)
    repository = MagicMock()
    orchestrator._conversation_repo = repository
    raw = _chat_item()
    item = await queue.enqueue(
        replace(raw, payload={**raw.payload, "conversation_id": str(uuid4())})
    )
    claimed = await queue.claim_next()
    with caplog.at_level("INFO"):
        await orchestrator._process(claimed)

    restarted = SqliteWorkQueue(db_path=tmp_path / "tasks.db")
    persisted = await restarted._load_item(str(item.id))
    assert persisted.status is TaskStatus.PENDING  # Existing bounded backoff unchanged.
    assert persisted.attempts == 1
    assert sink.emitted == []
    assert sink.closed and sink.closed[-1]["outcome"] == "failed"
    assert repository.append_message.called
    assert str(item.id) in caplog.text
    assert "hermes.tasks.loop.engine_error" in caplog.text
    assert SECRET not in caplog.text + str(persisted) + str(sink.closed) + str(
        repository.mock_calls
    )
    assert "chat_replied" not in caplog.text
    assert "task_completed" not in caplog.text
