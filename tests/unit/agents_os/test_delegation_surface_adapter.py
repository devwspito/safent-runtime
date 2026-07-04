"""DelegationSurfaceAdapter — FASE 3 (A2A cross-human), REQUESTER side.

Covers:
  - replay() POSTs {to_employee_id, to_agent_id="", body, correlation_id} to
    {cloud}/v1/outbox with a FRESH correlation_id, and records
    correlation_id -> conversation_id when the current task is chat-bound.
  - Missing employee_id/task -> failed (no HTTP call).
  - Surface mismatch -> rejected_by_policy.
  - Unassociated instance / missing secret / transport error -> failed
    (never raises).
  - An autonomous (non-chat) task never binds a correlation (non-fatal).
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.delegation_surface_adapter import (
    DelegationSurfaceAdapter,
)

pytestmark = pytest.mark.unit


def _fake_store(*, associated: bool = True, secret: str | None = "shh-secret") -> MagicMock:
    store = MagicMock()
    store.is_associated.return_value = associated
    assoc = MagicMock()
    assoc.cloud_endpoint = "https://cloud.example.com"
    store.get.return_value = assoc if associated else None
    store.reveal_instance_secret.return_value = secret
    return store


def _seed_chat_task(db_path, *, task_id: str, conversation_id: str) -> None:
    from hermes.tasks.infrastructure.schema import ensure_tasks_schema

    conn = sqlite3.connect(str(db_path))
    ensure_tasks_schema(conn)
    now = "2026-07-04T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO agent_tasks (
            task_id, trigger_kind, enqueued_by, tenant_id, operator_id,
            instruction, kind, conversation_id, status, created_at, updated_at
        ) VALUES (?, 'chat_message', ?, ?, ?, 'hi', 'chat_message', ?, 'pending', ?, ?)
        """,
        (task_id, str(uuid4()), str(uuid4()), str(uuid4()), conversation_id, now, now),
    )
    conn.commit()
    conn.close()


class TestReplay:
    @pytest.mark.asyncio
    async def test_posts_to_outbox_and_records_correlation(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        task_id = str(uuid4())
        conversation_id = str(uuid4())
        _seed_chat_task(db_path, task_id=task_id, conversation_id=conversation_id)

        from uuid import UUID

        adapter = DelegationSurfaceAdapter(
            association_store=_fake_store(), db_path=db_path,
        )
        action = CapturedAction(
            surface_kind=SurfaceKind.PEER_DELEGATION,
            intent_desc="ask a colleague",
            payload={"employee_id": "bob@org.example", "task": "review Q3 numbers"},
            work_item_id=UUID(task_id),
        )

        captured: list[dict] = []

        def _fake_post(url, *, headers, json, timeout, follow_redirects):  # noqa: A002
            captured.append({"url": url, "headers": headers, "body": json})
            return MagicMock(status_code=200)

        with patch("httpx.post", side_effect=_fake_post):
            outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_OK
        assert len(captured) == 1
        assert captured[0]["url"] == "https://cloud.example.com/v1/outbox"
        assert captured[0]["headers"]["Authorization"] == "Bearer shh-secret"
        body = captured[0]["body"]
        assert body["to_employee_id"] == "bob@org.example"
        assert body["to_agent_id"] == ""
        assert body["body"] == "review Q3 numbers"
        assert body["correlation_id"] == outcome.result["correlation_id"]

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT conversation_id FROM delegation_outbox_correlations "
            "WHERE correlation_id = ?",
            (outcome.result["correlation_id"],),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["conversation_id"] == conversation_id

    @pytest.mark.asyncio
    async def test_missing_employee_id_fails_without_http_call(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        adapter = DelegationSurfaceAdapter(association_store=_fake_store(), db_path=db_path)
        action = CapturedAction(
            surface_kind=SurfaceKind.PEER_DELEGATION,
            payload={"task": "no employee_id here"},
        )

        with patch("httpx.post") as mock_post:
            outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_task_fails_without_http_call(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        adapter = DelegationSurfaceAdapter(association_store=_fake_store(), db_path=db_path)
        action = CapturedAction(
            surface_kind=SurfaceKind.PEER_DELEGATION,
            payload={"employee_id": "bob@org.example"},
        )

        with patch("httpx.post") as mock_post:
            outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_surface_mismatch_is_rejected_by_policy(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        adapter = DelegationSurfaceAdapter(association_store=_fake_store(), db_path=db_path)
        action = CapturedAction(
            surface_kind=SurfaceKind.API_CALL,
            payload={"employee_id": "bob@org.example", "task": "x"},
        )

        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY

    @pytest.mark.asyncio
    async def test_unassociated_instance_fails_without_http_call(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        adapter = DelegationSurfaceAdapter(
            association_store=_fake_store(associated=False), db_path=db_path,
        )
        action = CapturedAction(
            surface_kind=SurfaceKind.PEER_DELEGATION,
            payload={"employee_id": "bob@org.example", "task": "x"},
        )

        with patch("httpx.post") as mock_post:
            outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_instance_secret_fails_without_http_call(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        adapter = DelegationSurfaceAdapter(
            association_store=_fake_store(secret=None), db_path=db_path,
        )
        action = CapturedAction(
            surface_kind=SurfaceKind.PEER_DELEGATION,
            payload={"employee_id": "bob@org.example", "task": "x"},
        )

        with patch("httpx.post") as mock_post:
            outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_transport_error_fails_never_raises(self, tmp_path) -> None:
        import httpx

        db_path = tmp_path / "state.db"
        adapter = DelegationSurfaceAdapter(association_store=_fake_store(), db_path=db_path)
        action = CapturedAction(
            surface_kind=SurfaceKind.PEER_DELEGATION,
            payload={"employee_id": "bob@org.example", "task": "x"},
        )

        with patch("httpx.post", side_effect=httpx.ConnectError("boom")):
            outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED

    @pytest.mark.asyncio
    async def test_autonomous_task_delegation_succeeds_without_conversation_binding(
        self, tmp_path
    ) -> None:
        """No agent_tasks row for this work_item_id (e.g. autonomous task) —
        the delegation still succeeds; only the correlation binding is skipped."""
        db_path = tmp_path / "state.db"
        adapter = DelegationSurfaceAdapter(association_store=_fake_store(), db_path=db_path)
        action = CapturedAction(
            surface_kind=SurfaceKind.PEER_DELEGATION,
            payload={"employee_id": "bob@org.example", "task": "x"},
            work_item_id=uuid4(),  # never seeded in agent_tasks
        )

        with patch("httpx.post", return_value=MagicMock(status_code=200)):
            outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_OK
        from hermes.config_sync import delegation_inbox as di

        conn = di._connect(db_path)  # noqa: SLF001 — test introspection
        di._ensure_schema(conn)  # noqa: SLF001
        conn.row_factory = sqlite3.Row
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM delegation_outbox_correlations"
        ).fetchone()["n"]
        conn.close()
        assert count == 0

    def test_serialize_for_signing_is_canonical(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        adapter = DelegationSurfaceAdapter(association_store=_fake_store(), db_path=db_path)
        action = CapturedAction(
            surface_kind=SurfaceKind.PEER_DELEGATION,
            intent_desc="ask",
            payload={"employee_id": "bob@org.example", "task": "x", "correlation_id": "c1"},
        )
        result = adapter.serialize_for_signing(action)
        assert b"correlation_id" not in result  # never signs a per-call nonce
        assert b"bob@org.example" in result
