"""Tests RemoteControlOrchestrator (FR-053..FR-056 BLOQUEANTES)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from hermes.agents_os.application.remote_control_orchestrator import (
    BindingViolationError,
    HumanConsentMissingError,
    RemoteControlEndReason,
    RemoteControlOrchestrator,
    RemoteControlScope,
    RemoteControlState,
    TokenCipher,
    TtlTooLongError,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher(key=os.urandom(32), kid="kid-test-1")


@pytest.fixture
def orch(cipher: TokenCipher) -> RemoteControlOrchestrator:
    return RemoteControlOrchestrator(cipher=cipher)


def _issue_defaults(orch: RemoteControlOrchestrator, **overrides):
    base = dict(
        node_installation_id=uuid4(),
        tenant_id=uuid4(),
        operator_id=uuid4(),
        scope=RemoteControlScope.OS_FULL_DESKTOP,
        dtls_fingerprint="aa:bb:cc:dd",
        consent_id=uuid4(),
        local_operator_approved=True,
        ttl_seconds=600,
    )
    base.update(overrides)
    return orch.issue(**base)


class TestIssue:
    def test_issue_returns_issued_state(
        self, orch: RemoteControlOrchestrator
    ) -> None:
        session = _issue_defaults(orch)
        assert session.state == RemoteControlState.ISSUED
        assert session.token_ciphertext
        assert session.binding_hash_hex

    def test_issue_without_local_consent_blocked(
        self, orch: RemoteControlOrchestrator
    ) -> None:
        with pytest.raises(HumanConsentMissingError):
            _issue_defaults(orch, local_operator_approved=False)

    def test_issue_ttl_over_60_min_blocked(
        self, orch: RemoteControlOrchestrator
    ) -> None:
        with pytest.raises(TtlTooLongError):
            _issue_defaults(orch, ttl_seconds=60 * 60 + 1)

    def test_issue_zero_ttl_blocked(
        self, orch: RemoteControlOrchestrator
    ) -> None:
        with pytest.raises(TtlTooLongError):
            _issue_defaults(orch, ttl_seconds=0)


class TestEncryption:
    def test_token_can_decrypt_with_correct_binding(
        self, orch: RemoteControlOrchestrator, cipher: TokenCipher
    ) -> None:
        session = _issue_defaults(orch)
        plaintext = cipher.decrypt(
            session.token_ciphertext,
            aad=session.binding_hash_hex.encode("ascii"),
            expected_kid=session.token_kid,
        )
        assert len(plaintext) == 48

    def test_token_decrypt_fails_with_wrong_binding(
        self, orch: RemoteControlOrchestrator, cipher: TokenCipher
    ) -> None:
        from cryptography.exceptions import InvalidTag

        session = _issue_defaults(orch)
        with pytest.raises(InvalidTag):
            cipher.decrypt(
                session.token_ciphertext,
                aad=b"wrong-binding",
                expected_kid=session.token_kid,
            )


class TestBinding:
    def test_reconnect_same_fingerprint_ok(
        self, orch: RemoteControlOrchestrator
    ) -> None:
        session = _issue_defaults(orch, dtls_fingerprint="ab:cd")
        orch.verify_reconnect_binding(
            session, observed_dtls_fingerprint="ab:cd"
        )

    def test_reconnect_different_fingerprint_blocked(
        self, orch: RemoteControlOrchestrator
    ) -> None:
        session = _issue_defaults(orch, dtls_fingerprint="ab:cd")
        with pytest.raises(BindingViolationError):
            orch.verify_reconnect_binding(
                session, observed_dtls_fingerprint="ee:ff"
            )


class TestLifecycle:
    def test_issued_to_accepted(
        self, orch: RemoteControlOrchestrator
    ) -> None:
        session = _issue_defaults(orch)
        accepted = orch.accept(session)
        assert accepted.state == RemoteControlState.ACCEPTED
        assert accepted.accepted_at is not None

    def test_accept_after_expiry_marks_ended_timeout(
        self, cipher: TokenCipher
    ) -> None:
        clock_t = {"now": datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)}
        orch = RemoteControlOrchestrator(
            cipher=cipher, clock=lambda: clock_t["now"]
        )
        session = _issue_defaults(orch, ttl_seconds=60)
        # Avanza 2 minutos.
        clock_t["now"] = clock_t["now"] + timedelta(minutes=2)
        ended = orch.accept(session)
        assert ended.state == RemoteControlState.ENDED
        assert ended.end_reason == RemoteControlEndReason.TIMEOUT

    def test_accepted_to_active(self, orch: RemoteControlOrchestrator) -> None:
        session = _issue_defaults(orch)
        active = orch.activate(orch.accept(session))
        assert active.state == RemoteControlState.ACTIVE

    def test_end_with_reason(self, orch: RemoteControlOrchestrator) -> None:
        session = _issue_defaults(orch)
        ended = orch.end(
            session, RemoteControlEndReason.TENANT_REVOKED
        )
        assert ended.state == RemoteControlState.ENDED
        assert ended.end_reason == RemoteControlEndReason.TENANT_REVOKED
        assert ended.ended_at is not None
