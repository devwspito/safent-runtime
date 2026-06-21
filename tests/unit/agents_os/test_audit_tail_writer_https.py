"""Regression tests: AuditTailWriter + HttpsAuditTailTransport (finding #28).

Before the fix:
  - HttpsAuditTailTransport did not exist.
  - AuditTailWriter was never instantiated in production code.

This verifies:
1. HttpsAuditTailTransport is importable and satisfies AuditTailTransport.
2. AuditTailWriter with FakeTransport start_background + stop works.
3. Stats endpoint returns correct fields.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import (
    AuditHashChainSigner,
    AuditKind,
)
from hermes.agents_os.infrastructure.audit_tail_writer import (
    AuditTailTransport,
    AuditTailWriter,
    FakeAuditTailTransport,
    HttpsAuditTailTransport,
    TailPublishError,
)

pytestmark = pytest.mark.unit


class TestHttpsAuditTailTransport:
    def test_is_importable(self) -> None:
        """HttpsAuditTailTransport is importable (was missing before fix)."""
        transport = HttpsAuditTailTransport(url="https://cp.example.com/audit")
        assert transport is not None

    def test_satisfies_protocol(self) -> None:
        """HttpsAuditTailTransport satisfies AuditTailTransport Protocol."""
        transport = HttpsAuditTailTransport(url="https://cp.example.com/audit")
        assert isinstance(transport, AuditTailTransport)

    def test_raises_tail_publish_error_on_network_failure(self) -> None:
        """publish() raises TailPublishError on connection error (no httpx mock needed)."""
        transport = HttpsAuditTailTransport(
            url="http://127.0.0.1:19999/unreachable",
            timeout=0.1,
        )
        with pytest.raises(TailPublishError):
            transport.publish(entries=[{"entry_id": "test"}])


class TestAuditTailWriterWiring:
    def test_start_background_and_stop(self, tmp_path: Path) -> None:
        """Writer starts background thread and stops cleanly."""
        transport = FakeAuditTailTransport()
        writer = AuditTailWriter(
            transport=transport,
            spool_dir=tmp_path / "spool",
            batch_size=10,
        )
        writer.start_background(flush_interval_seconds=0.1)
        time.sleep(0.25)
        writer.stop()
        stats = writer.stats()
        assert stats is not None

    def test_enqueue_and_flush_publishes(self) -> None:
        """Entries enqueued are published via transport on flush_once."""
        signing_key = secrets.token_bytes(32)
        signer = AuditHashChainSigner(signing_key=signing_key)
        entry = signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor="test-operator",
            description="test grant",
            payload={"capability": "documents"},
        )
        transport = FakeAuditTailTransport()
        writer = AuditTailWriter(transport=transport, spool_dir=None)
        writer.enqueue(entry)
        published = writer.flush_once()
        assert published == 1
        assert len(transport.published) == 1

    def test_stats_reflect_published_count(self, tmp_path: Path) -> None:
        signing_key = secrets.token_bytes(32)
        signer = AuditHashChainSigner(signing_key=signing_key)
        entry = signer.append(
            audit_kind=AuditKind.CONSENT_REVOKED,
            actor="op",
            description="revoke",
            payload={"capability": "screen"},
        )
        transport = FakeAuditTailTransport()
        writer = AuditTailWriter(transport=transport, spool_dir=tmp_path / "spool")
        writer.enqueue(entry)
        writer.flush_once()
        stats = writer.stats()
        assert stats.published_total == 1
        assert stats.failures_total == 0
        assert stats.queued_in_memory == 0
