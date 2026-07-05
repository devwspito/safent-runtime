"""Tests for UsageUploader and the unsent_aggregates / mark_uploaded repo methods.

Privacy invariant test: the POST body NEVER contains content/PII/prompt/URL keys.
This test is the enforced gate for the privacy boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from hermes.config_sync.uploader import (
    PROHIBITED_BODY_KEYS,
    UploadResult,
    UsageUploader,
    _build_payload,
)
from hermes.domain.cycle_output import TokenUsage
from hermes.instance.association_store import InstanceAssociation
from hermes.shell_server.metering.usage_repo import SQLiteUsageRepository, UnsentAggregate

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> SQLiteUsageRepository:
    return SQLiteUsageRepository(db_path=tmp_path / "usage.db")


def _make_usage(
    *,
    prompt: int = 100,
    completion: int = 50,
    cost: float = 0.001,
    model: str = "qwen3",
) -> TokenUsage:
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost_usd=cost,
        model=model,
        cost_status="billed",
        cost_source="litellm",
        provider="vllm",
    )


def _record(repo: SQLiteUsageRepository, *, agent_id: str = "agent-1", outcome: str = "completed") -> None:
    repo.record_cycle(
        agent_id=agent_id,
        conversation_id="conv-1",
        task_id="task-1",
        usage=_make_usage(),
        tool_calls=1,
        latency_ms=100,
        outcome=outcome,
    )


@pytest.fixture
def fake_store_associated():
    """In-memory fake that mimics SQLiteAssociationStore — associated."""
    store = MagicMock()
    store.is_associated.return_value = True
    store.edition.return_value = "associate"
    store.get.return_value = InstanceAssociation(
        instance_id="inst-abc",
        tenant_id="tenant-xyz",
        paired_at="2026-06-26T10:00:00Z",
        cloud_endpoint="https://cloud.safent.run",
        signing_pubkey_hex="a" * 64,
        license={},
        last_applied_version=0,
        state="active",
    )
    store.reveal_instance_secret.return_value = "sk-test-secret"
    return store


@pytest.fixture
def fake_store_community():
    """In-memory fake — community edition (not associated)."""
    store = MagicMock()
    store.is_associated.return_value = False
    store.edition.return_value = "community"
    return store


# ---------------------------------------------------------------------------
# SQLiteUsageRepository — unsent_aggregates / mark_uploaded
# ---------------------------------------------------------------------------


class TestUnsentAggregates:
    def test_empty_repo_returns_empty(self, repo: SQLiteUsageRepository) -> None:
        assert repo.unsent_aggregates() == []

    def test_single_event_appears_as_aggregate(self, repo: SQLiteUsageRepository) -> None:
        _record(repo)
        aggs = repo.unsent_aggregates()
        assert len(aggs) == 1
        agg = aggs[0]
        assert agg.agent_id == "agent-1"
        assert agg.prompt_tokens == 100
        assert agg.completion_tokens == 50
        assert agg.tasks == 1
        assert agg.failures == 0
        assert len(agg.event_ids) == 1

    def test_two_events_same_agent_day_aggregate(self, repo: SQLiteUsageRepository) -> None:
        _record(repo, agent_id="agent-1")
        _record(repo, agent_id="agent-1")
        aggs = repo.unsent_aggregates()
        assert len(aggs) == 1
        agg = aggs[0]
        assert agg.tasks == 2
        assert agg.prompt_tokens == 200
        assert len(agg.event_ids) == 2

    def test_different_agents_produce_separate_aggregates(self, repo: SQLiteUsageRepository) -> None:
        _record(repo, agent_id="agent-A")
        _record(repo, agent_id="agent-B")
        aggs = repo.unsent_aggregates()
        agent_ids = {a.agent_id for a in aggs}
        assert agent_ids == {"agent-A", "agent-B"}

    def test_failure_outcome_counted_in_failures(self, repo: SQLiteUsageRepository) -> None:
        _record(repo, outcome="completed")
        _record(repo, outcome="failed")
        aggs = repo.unsent_aggregates()
        assert len(aggs) == 1
        assert aggs[0].failures == 1
        assert aggs[0].tasks == 2

    def test_already_uploaded_excluded_from_aggregates(self, repo: SQLiteUsageRepository) -> None:
        _record(repo, agent_id="agent-1")
        aggs = repo.unsent_aggregates()
        ids = list(aggs[0].event_ids)
        repo.mark_uploaded(ids)
        assert repo.unsent_aggregates() == []

    def test_partial_upload_only_pending_returned(self, repo: SQLiteUsageRepository) -> None:
        _record(repo, agent_id="agent-1")
        _record(repo, agent_id="agent-1")
        aggs = repo.unsent_aggregates()
        # Mark only first event
        repo.mark_uploaded([aggs[0].event_ids[0]])
        remaining = repo.unsent_aggregates()
        assert len(remaining) == 1
        assert remaining[0].tasks == 1

    def test_mark_uploaded_is_idempotent(self, repo: SQLiteUsageRepository) -> None:
        _record(repo)
        aggs = repo.unsent_aggregates()
        ids = list(aggs[0].event_ids)
        repo.mark_uploaded(ids)
        repo.mark_uploaded(ids)  # second call must not raise
        assert repo.unsent_aggregates() == []

    def test_mark_uploaded_empty_list_is_noop(self, repo: SQLiteUsageRepository) -> None:
        _record(repo)
        repo.mark_uploaded([])
        assert len(repo.unsent_aggregates()) == 1

    def test_migration_adds_uploaded_column_to_existing_db(self, tmp_path: Path) -> None:
        """Simulates upgrading a pre-Fase-5 DB that lacks the 'uploaded' column."""
        import sqlite3

        db_path = tmp_path / "old.db"
        # Create DB without 'uploaded' column (legacy schema)
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE usage_events (
              event_id TEXT PRIMARY KEY,
              ts TEXT NOT NULL,
              agent_id TEXT,
              conversation_id TEXT,
              task_id TEXT,
              provider TEXT,
              model TEXT NOT NULL,
              prompt_tokens INTEGER NOT NULL DEFAULT 0,
              completion_tokens INTEGER NOT NULL DEFAULT 0,
              total_tokens INTEGER NOT NULL DEFAULT 0,
              cost_usd REAL NOT NULL DEFAULT 0.0,
              tool_calls INTEGER NOT NULL DEFAULT 0,
              latency_ms INTEGER,
              outcome TEXT NOT NULL DEFAULT 'completed',
              cost_status TEXT NOT NULL DEFAULT 'unknown',
              cost_source TEXT NOT NULL DEFAULT 'none'
            )
        """)
        conn.commit()
        conn.close()

        # Opening via SQLiteUsageRepository must silently migrate
        repo2 = SQLiteUsageRepository(db_path=db_path)
        # Post-migration: unsent_aggregates() must work (not crash on missing column)
        assert repo2.unsent_aggregates() == []


# ---------------------------------------------------------------------------
# UsageUploader — gate / no-op cases
# ---------------------------------------------------------------------------


class TestUploaderGates:
    def test_community_edition_is_noop(
        self, repo: SQLiteUsageRepository, fake_store_community: MagicMock
    ) -> None:
        uploader = UsageUploader(usage_repo=repo, association_store=fake_store_community)
        result = uploader.upload_once()
        assert result.uploaded_items == 0
        assert result.skipped_reason == "not_associated"

    def test_associated_but_no_data_is_noop(
        self, repo: SQLiteUsageRepository, fake_store_associated: MagicMock
    ) -> None:
        uploader = UsageUploader(usage_repo=repo, association_store=fake_store_associated)
        with patch("httpx.post") as mock_post:
            result = uploader.upload_once()
        mock_post.assert_not_called()
        assert result.skipped_reason == "no_data"

    def test_no_instance_secret_is_noop(
        self, repo: SQLiteUsageRepository, fake_store_associated: MagicMock
    ) -> None:
        fake_store_associated.reveal_instance_secret.return_value = None
        _record(repo)
        uploader = UsageUploader(usage_repo=repo, association_store=fake_store_associated)
        with patch("httpx.post") as mock_post:
            result = uploader.upload_once()
        mock_post.assert_not_called()
        assert result.skipped_reason == "no_secret"


# ---------------------------------------------------------------------------
# UsageUploader — successful upload path
# ---------------------------------------------------------------------------


class TestUploaderSuccess:
    def test_posts_correct_body_and_marks_uploaded(
        self, repo: SQLiteUsageRepository, fake_store_associated: MagicMock
    ) -> None:
        _record(repo, agent_id="agent-1")
        uploader = UsageUploader(usage_repo=repo, association_store=fake_store_associated)

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            result = uploader.upload_once()

        assert result.uploaded_items == 1
        assert result.skipped_reason is None

        # Verify events are now marked as uploaded
        assert repo.unsent_aggregates() == []

        # Inspect the POST call
        call_kwargs = mock_post.call_args
        url = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("url", "")
        assert "/v1/metering" in url
        body = call_kwargs.kwargs.get("json", {})
        assert body["instance_id"] == "inst-abc"
        assert body["tenant_id"] == "tenant-xyz"
        assert isinstance(body["items"], list)
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["agent_id"] == "agent-1"
        assert item["prompt_tokens"] == 100
        assert item["completion_tokens"] == 50
        assert item["tasks"] == 1
        assert item["failures"] == 0

    def test_bearer_token_sent(
        self, repo: SQLiteUsageRepository, fake_store_associated: MagicMock
    ) -> None:
        _record(repo)
        uploader = UsageUploader(usage_repo=repo, association_store=fake_store_associated)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 201

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            uploader.upload_once()

        headers = mock_post.call_args.kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer sk-test-secret"

    def test_no_redirect_follow(
        self, repo: SQLiteUsageRepository, fake_store_associated: MagicMock
    ) -> None:
        """SSRF mitigation: follow_redirects must be False."""
        _record(repo)
        uploader = UsageUploader(usage_repo=repo, association_store=fake_store_associated)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            uploader.upload_once()

        assert mock_post.call_args.kwargs.get("follow_redirects") is False


# ---------------------------------------------------------------------------
# UsageUploader — fail-soft on network/HTTP errors
# ---------------------------------------------------------------------------


class TestUploaderFailSoft:
    def test_network_error_does_not_mark_uploaded(
        self, repo: SQLiteUsageRepository, fake_store_associated: MagicMock
    ) -> None:
        _record(repo)
        uploader = UsageUploader(usage_repo=repo, association_store=fake_store_associated)

        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            result = uploader.upload_once()

        assert result.skipped_reason == "network_error"
        # Events must still be unsent (retry next tick)
        assert len(repo.unsent_aggregates()) == 1

    def test_http_5xx_does_not_mark_uploaded(
        self, repo: SQLiteUsageRepository, fake_store_associated: MagicMock
    ) -> None:
        _record(repo)
        uploader = UsageUploader(usage_repo=repo, association_store=fake_store_associated)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 503

        with patch("httpx.post", return_value=mock_resp):
            result = uploader.upload_once()

        assert result.skipped_reason == "http_503"
        assert len(repo.unsent_aggregates()) == 1

    def test_http_4xx_does_not_mark_uploaded(
        self, repo: SQLiteUsageRepository, fake_store_associated: MagicMock
    ) -> None:
        _record(repo)
        uploader = UsageUploader(usage_repo=repo, association_store=fake_store_associated)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 401

        with patch("httpx.post", return_value=mock_resp):
            result = uploader.upload_once()

        assert result.skipped_reason == "http_401"
        assert len(repo.unsent_aggregates()) == 1


# ---------------------------------------------------------------------------
# PRIVACY INVARIANT — the serialized body must never contain content keys
# ---------------------------------------------------------------------------


class TestPrivacyInvariant:
    """Hard gate: the upload body MUST NOT contain any content/PII/prompt key.

    This test is the enforcement boundary for the telemetry privacy guarantee.
    It must remain in the test suite permanently and must never be relaxed.
    """

    def _serialize_body(self, aggregates: list[UnsentAggregate]) -> str:
        body = _build_payload("inst-1", "tenant-1", aggregates)
        return json.dumps(body, default=str).lower()

    def test_empty_aggregates_body_has_no_prohibited_keys(self) -> None:
        serialized = self._serialize_body([])
        for key in PROHIBITED_BODY_KEYS:
            assert key not in serialized, (
                f"Prohibited key '{key}' found in upload body with empty aggregates"
            )

    def test_typical_aggregates_body_has_no_prohibited_keys(self) -> None:
        agg = UnsentAggregate(
            agent_id="agent-secret-name",
            day="2026-06-26",
            prompt_tokens=1000,
            completion_tokens=500,
            cost_usd=0.005,
            tasks=10,
            failures=1,
            event_ids=("evt-1", "evt-2"),
        )
        serialized = self._serialize_body([agg])
        for key in PROHIBITED_BODY_KEYS:
            # Use word-boundary check: "agent_id" contains "agent" but that is allowed;
            # we check the key as a standalone JSON key (preceded by '"').
            assert f'"{key}"' not in serialized, (
                f"Prohibited key '{key}' found as JSON key in upload body"
            )

    def test_prohibited_keys_list_is_comprehensive(self) -> None:
        """Document the full set and catch accidental removal."""
        expected = {
            "content", "prompt", "message", "text", "url", "file",
            "secret", "api_key", "password", "token", "conversation", "response",
        }
        assert expected.issubset(PROHIBITED_BODY_KEYS), (
            f"Missing prohibited keys: {expected - PROHIBITED_BODY_KEYS}"
        )

    def test_actual_post_body_has_no_prohibited_keys(
        self, repo: SQLiteUsageRepository, fake_store_associated: MagicMock
    ) -> None:
        """End-to-end: record events, run uploader, capture actual POST body."""
        _record(repo, agent_id="agent-alpha")
        _record(repo, agent_id="agent-beta")
        uploader = UsageUploader(usage_repo=repo, association_store=fake_store_associated)

        captured_body: dict[str, Any] = {}
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200

        def capture_post(url: str, *, json: Any = None, **kwargs: Any) -> MagicMock:
            captured_body.update(json or {})
            return mock_resp

        with patch("httpx.post", side_effect=capture_post):
            uploader.upload_once()

        serialized = json.dumps(captured_body, default=str).lower()
        for key in PROHIBITED_BODY_KEYS:
            assert f'"{key}"' not in serialized, (
                f"Prohibited key '{key}' found in actual POST body to /v1/metering"
            )
