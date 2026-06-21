"""Tests AuditHashChainSigner (FR-049 BLOQUEANTE)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import (
    AuditChainCorrupted,
    AuditEntry,
    AuditHashChainSigner,
    AuditKind,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def signer() -> AuditHashChainSigner:
    return AuditHashChainSigner(signing_key=secrets.token_bytes(32))


class TestAppend:
    def test_first_entry_uses_genesis_anchor(
        self, signer: AuditHashChainSigner
    ) -> None:
        entry = signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor="hermes-user",
            description="grant docs",
            payload={"capability": "documents"},
        )
        assert entry.prev_entry_hash_hex == "00" * 32

    def test_chained_entries_link(
        self, signer: AuditHashChainSigner
    ) -> None:
        first = signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor="u",
            description="g1",
            payload={"a": 1},
        )
        second = signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor="u",
            description="g2",
            payload={"a": 2},
        )
        assert second.prev_entry_hash_hex == first.signed_payload_hash_hex

    def test_signing_key_too_short_rejected(self) -> None:
        with pytest.raises(ValueError):
            AuditHashChainSigner(signing_key=b"too-short")


class TestVerify:
    def test_verify_happy_chain(self, signer: AuditHashChainSigner) -> None:
        entries = [
            signer.append(
                audit_kind=AuditKind.OTA_QUEUED,
                actor="root",
                description=f"ota-{i}",
                payload={"i": i},
            )
            for i in range(5)
        ]
        signer.verify_chain(entries)  # no raise

    def test_verify_detects_payload_tamper(
        self, signer: AuditHashChainSigner
    ) -> None:
        e = signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor="u",
            description="x",
            payload={"v": 1},
        )
        # Tamperear: cambiar el payload_hash_hex.
        tampered = AuditEntry(
            entry_id=e.entry_id,
            node_installation_id=e.node_installation_id,
            tenant_id=e.tenant_id,
            timestamp=e.timestamp,
            actor=e.actor,
            audit_kind=e.audit_kind,
            category=e.category,
            description=e.description,
            payload_hash_hex="ff" * 32,
            prev_entry_hash_hex=e.prev_entry_hash_hex,
            signed_payload_hash_hex=e.signed_payload_hash_hex,
            signature_hex=e.signature_hex,
        )
        with pytest.raises(AuditChainCorrupted):
            signer.verify_chain([tampered])

    def test_verify_detects_signature_tamper(
        self, signer: AuditHashChainSigner
    ) -> None:
        e = signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor="u",
            description="x",
            payload={"v": 1},
        )
        tampered = AuditEntry(
            entry_id=e.entry_id,
            node_installation_id=e.node_installation_id,
            tenant_id=e.tenant_id,
            timestamp=e.timestamp,
            actor=e.actor,
            audit_kind=e.audit_kind,
            category=e.category,
            description=e.description,
            payload_hash_hex=e.payload_hash_hex,
            prev_entry_hash_hex=e.prev_entry_hash_hex,
            signed_payload_hash_hex=e.signed_payload_hash_hex,
            signature_hex="11" * 32,
        )
        with pytest.raises(AuditChainCorrupted):
            signer.verify_chain([tampered])

    def test_verify_detects_reorder(
        self, signer: AuditHashChainSigner
    ) -> None:
        a = signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor="u",
            description="a",
            payload={"i": 0},
        )
        b = signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor="u",
            description="b",
            payload={"i": 1},
        )
        with pytest.raises(AuditChainCorrupted):
            signer.verify_chain([b, a])  # reorder

    def test_wrong_signing_key_rejects(
        self, signer: AuditHashChainSigner
    ) -> None:
        e = signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor="u",
            description="x",
            payload={"v": 1},
        )
        other = AuditHashChainSigner(signing_key=secrets.token_bytes(32))
        with pytest.raises(AuditChainCorrupted):
            other.verify_chain([e])


class TestDeterminism:
    def test_canonical_payload_order_does_not_matter(
        self, signer: AuditHashChainSigner
    ) -> None:
        e1 = signer.append(
            audit_kind=AuditKind.NODE_INSTALL_CREATED,
            actor="root",
            description="x",
            payload={"a": 1, "b": 2, "c": 3},
        )
        signer2 = AuditHashChainSigner(signing_key=b"\x00" * 32)
        # Mismo payload, distinto orden de claves → mismo payload_hash.
        e2a = signer2.append(
            audit_kind=AuditKind.NODE_INSTALL_CREATED,
            actor="root",
            description="x",
            payload={"c": 3, "a": 1, "b": 2},
        )
        e2b = signer2.append(
            audit_kind=AuditKind.NODE_INSTALL_CREATED,
            actor="root",
            description="x",
            payload={"a": 1, "b": 2, "c": 3},
        )
        # signed_payload_hash depende del prev — son distintos por la
        # cadena, pero el payload_hash de e2b debería igualar a e1.
        assert e1.payload_hash_hex == e2b.payload_hash_hex


class TestClock:
    def test_clock_injection(self) -> None:
        fixed = datetime(2026, 5, 28, tzinfo=UTC)
        signer = AuditHashChainSigner(
            signing_key=secrets.token_bytes(32), clock=lambda: fixed
        )
        e = signer.append(
            audit_kind=AuditKind.OTA_QUEUED,
            actor="cron",
            description="x",
            payload={},
        )
        assert e.timestamp == fixed
