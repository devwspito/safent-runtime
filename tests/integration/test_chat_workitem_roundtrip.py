"""T-FIX-001 — Roundtrip chat WorkItem via SQLite (FR-010 / SC-004 / SC-006 / G4).

Verifica que SqliteWorkQueue persiste y reconstruye correctamente:
  - kind = CHAT_MESSAGE (I5 invariante del data-model 006)
  - conversation_id (I5: chat_message ⇒ conversation_id NOT NULL)

El bug original: enqueue() no escribía las columnas `kind` ni `conversation_id`,
y _load_item() no las leía — el item reconstruido tenía kind=AUTONOMOUS y
conversation_id ausente del payload. Consecuencia: AgentLoopOrchestrator._process()
trataba SIEMPRE is_chat=False, matando la rama de chat completa.

Este test DEBE FALLAR antes del fix y PASAR después.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hermes.tasks.domain.ports import WorkItem, WorkItemKind
from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue

pytestmark = pytest.mark.integration

_TENANT_ID = UUID("12345678-1234-5678-1234-567812345678")
_CONV_ID = "conv-abc-123"


def _make_queue(tmp_path: Path) -> SqliteWorkQueue:
    return SqliteWorkQueue(db_path=tmp_path / "test-tasks.db")


class TestChatWorkItemRoundtrip:
    """Verifica que kind + conversation_id sobreviven el ciclo enqueue→claim_next."""

    async def test_kind_preserved_after_enqueue_and_claim(self, tmp_path: Path) -> None:
        """WorkItem(kind=CHAT_MESSAGE) encolado → reclamado conserva kind=CHAT_MESSAGE.

        Falla antes del fix porque enqueue no insertaba la columna `kind`
        y _load_item no la leía (quedaba en DEFAULT 'autonomous').
        """
        queue = _make_queue(tmp_path)
        item = WorkItem.new(
            tenant_id=_TENANT_ID,
            trigger_kind="chat_message",
            kind=WorkItemKind.CHAT_MESSAGE,
            payload={
                "enqueued_by": str(UUID(int=1000)),
                "instruction": "hola agente",
                "conversation_id": _CONV_ID,
            },
        )

        await queue.enqueue(item)
        claimed = await queue.claim_next()

        assert claimed is not None, "claim_next() debe devolver el item encolado"
        assert claimed.kind is WorkItemKind.CHAT_MESSAGE, (
            f"kind debe ser CHAT_MESSAGE, pero es {claimed.kind!r}. "
            "El bug: enqueue no persistía `kind` y _load_item no la leía."
        )

    async def test_conversation_id_preserved_after_roundtrip(self, tmp_path: Path) -> None:
        """conversation_id del payload sobrevive el ciclo enqueue→claim_next.

        El conversation_id viaja en el payload_json. Este test confirma que
        el payload_json completo se persiste y reconstruye correctamente.
        """
        queue = _make_queue(tmp_path)
        conv_id = f"conv-{uuid4()}"
        item = WorkItem.new(
            tenant_id=_TENANT_ID,
            trigger_kind="chat_message",
            kind=WorkItemKind.CHAT_MESSAGE,
            payload={
                "enqueued_by": str(UUID(int=1000)),
                "instruction": "mensaje de prueba",
                "conversation_id": conv_id,
            },
        )

        await queue.enqueue(item)
        claimed = await queue.claim_next()

        assert claimed is not None
        assert claimed.payload.get("conversation_id") == conv_id, (
            f"conversation_id debe ser {conv_id!r}, "
            f"pero es {claimed.payload.get('conversation_id')!r}"
        )

    async def test_autonomous_item_kind_preserved(self, tmp_path: Path) -> None:
        """WorkItem(kind=AUTONOMOUS) conserva kind=AUTONOMOUS tras roundtrip.

        Regresión: el fix de CHAT_MESSAGE no debe romper los items autónomos.
        """
        queue = _make_queue(tmp_path)
        item = WorkItem.new(
            tenant_id=_TENANT_ID,
            trigger_kind="manual_enqueue",
            kind=WorkItemKind.AUTONOMOUS,
            payload={"enqueued_by": str(UUID(int=1000))},
        )

        await queue.enqueue(item)
        claimed = await queue.claim_next()

        assert claimed is not None
        assert claimed.kind is WorkItemKind.AUTONOMOUS, (
            f"kind debe ser AUTONOMOUS, pero es {claimed.kind!r}"
        )

    async def test_chat_item_kind_loaded_by_task_by_id(self, tmp_path: Path) -> None:
        """task_by_id también reconstruye kind=CHAT_MESSAGE correctamente."""
        queue = _make_queue(tmp_path)
        item = WorkItem.new(
            tenant_id=_TENANT_ID,
            trigger_kind="chat_message",
            kind=WorkItemKind.CHAT_MESSAGE,
            payload={
                "enqueued_by": str(UUID(int=1000)),
                "instruction": "vía task_by_id",
                "conversation_id": _CONV_ID,
            },
        )

        persisted = await queue.enqueue(item)
        loaded = await queue.task_by_id(task_id=persisted.id)

        assert loaded is not None
        assert loaded.kind is WorkItemKind.CHAT_MESSAGE, (
            f"task_by_id debe devolver kind=CHAT_MESSAGE, pero es {loaded.kind!r}"
        )
