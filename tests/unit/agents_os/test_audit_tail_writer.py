"""Tests AuditTailWriter (FR-049 publishes audit chain al control plane)."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from hermes.agents_os.application.audit_hash_chain import (
    AuditHashChainSigner,
    AuditKind,
)
from hermes.agents_os.infrastructure.audit_tail_writer import (
    AuditTailWriter,
    FakeAuditTailTransport,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def signer() -> AuditHashChainSigner:
    return AuditHashChainSigner(signing_key=secrets.token_bytes(32))


@pytest.fixture
def transport() -> FakeAuditTailTransport:
    return FakeAuditTailTransport()


def _append_entries(signer: AuditHashChainSigner, count: int):
    return [
        signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor=f"actor-{i}",
            description=f"event-{i}",
            payload={"i": i},
        )
        for i in range(count)
    ]


class TestFlush:
    def test_flush_publishes_batch(
        self,
        signer: AuditHashChainSigner,
        transport: FakeAuditTailTransport,
    ) -> None:
        writer = AuditTailWriter(transport=transport, batch_size=3)
        for e in _append_entries(signer, 3):
            writer.enqueue(e)
        published = writer.flush_once()
        assert published == 3
        assert len(transport.published) == 3

    def test_flush_partial_batch(
        self,
        signer: AuditHashChainSigner,
        transport: FakeAuditTailTransport,
    ) -> None:
        writer = AuditTailWriter(transport=transport, batch_size=10)
        for e in _append_entries(signer, 2):
            writer.enqueue(e)
        assert writer.flush_once() == 2

    def test_flush_empty_returns_zero(
        self, transport: FakeAuditTailTransport
    ) -> None:
        writer = AuditTailWriter(transport=transport)
        assert writer.flush_once() == 0


class TestFailureSpool:
    def test_failure_spools_to_disk(
        self,
        signer: AuditHashChainSigner,
        transport: FakeAuditTailTransport,
        tmp_path: Path,
    ) -> None:
        transport.fail_count = 1
        writer = AuditTailWriter(
            transport=transport, spool_dir=tmp_path / "spool"
        )
        for e in _append_entries(signer, 2):
            writer.enqueue(e)
        published = writer.flush_once()
        assert published == 0
        stats = writer.stats()
        assert stats.persisted_pending == 2
        assert stats.failures_total == 1

    def test_failure_without_spool_requeues(
        self,
        signer: AuditHashChainSigner,
        transport: FakeAuditTailTransport,
    ) -> None:
        transport.fail_count = 1
        writer = AuditTailWriter(transport=transport, spool_dir=None)
        for e in _append_entries(signer, 2):
            writer.enqueue(e)
        # Primer flush falla y re-enqueue.
        writer.flush_once()
        # Segundo flush ahora pasa.
        assert writer.flush_once() == 2

    def test_spool_recovers_on_init(
        self,
        signer: AuditHashChainSigner,
        transport: FakeAuditTailTransport,
        tmp_path: Path,
    ) -> None:
        spool = tmp_path / "spool"
        # Round 1: writer falla → spool.
        transport.fail_count = 1
        writer1 = AuditTailWriter(
            transport=transport, spool_dir=spool
        )
        for e in _append_entries(signer, 2):
            writer1.enqueue(e)
        writer1.flush_once()
        assert writer1.stats().persisted_pending == 2

        # Round 2: writer NUEVO con el mismo spool → recupera.
        transport.fail_count = 0
        writer2 = AuditTailWriter(
            transport=transport, spool_dir=spool
        )
        assert writer2.stats().queued_in_memory == 2
        assert writer2.flush_once() == 2
        # Spool vacío tras éxito.
        assert writer2.stats().persisted_pending == 0


class TestStats:
    def test_stats_track_totals(
        self,
        signer: AuditHashChainSigner,
        transport: FakeAuditTailTransport,
    ) -> None:
        writer = AuditTailWriter(transport=transport)
        for e in _append_entries(signer, 5):
            writer.enqueue(e)
        writer.flush_once()
        assert writer.stats().published_total == 5
        assert writer.stats().last_publish_at is not None
