"""Tests OtaOrchestrator (FR-008, FR-009, FR-050 BLOQUEANTES)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from hermes.agents_os.application.ota_orchestrator import (
    OtaAttemptState,
    OtaImageRejected,
    OtaOrchestrator,
    OtaRejectionReason,
    OtaRollbackReason,
    OtaStateTransitionError,
    RevocationList,
    assert_transition,
    is_strict_upgrade,
    parse_semver,
)

pytestmark = pytest.mark.unit


def _fresh_revocation_list(
    *, revoked: tuple[str, ...] = (), age_days: int = 0
) -> RevocationList:
    return RevocationList(
        revoked_versions=frozenset(revoked),
        refreshed_at=datetime.now(tz=UTC) - timedelta(days=age_days),
        signature_hex="a" * 64,
    )


class TestSemver:
    def test_parse_basic(self) -> None:
        assert parse_semver("1.2.3") == (1, 2, 3)
        assert parse_semver("v1.2.3") == (1, 2, 3)
        assert parse_semver("agents-os-v0.4.7") == (0, 4, 7)

    def test_parse_with_suffix(self) -> None:
        assert parse_semver("1.2.3-beta") == (1, 2, 3)

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_semver("not-a-version")

    def test_strict_upgrade_true(self) -> None:
        assert is_strict_upgrade("1.0.1", "1.0.0")
        assert is_strict_upgrade("2.0.0", "1.9.9")

    def test_strict_upgrade_false_same(self) -> None:
        assert not is_strict_upgrade("1.0.0", "1.0.0")

    def test_strict_upgrade_false_downgrade(self) -> None:
        assert not is_strict_upgrade("1.0.0", "1.0.1")


class TestStateMachine:
    def test_queued_to_downloading(self) -> None:
        assert_transition(OtaAttemptState.QUEUED, OtaAttemptState.DOWNLOADING)

    def test_invalid_transitions_blocked(self) -> None:
        with pytest.raises(OtaStateTransitionError):
            assert_transition(OtaAttemptState.QUEUED, OtaAttemptState.PROMOTED)
        with pytest.raises(OtaStateTransitionError):
            assert_transition(OtaAttemptState.STAGED, OtaAttemptState.DOWNLOADING)

    def test_terminal_states_are_truly_terminal(self) -> None:
        for terminal in (
            OtaAttemptState.PROMOTED,
            OtaAttemptState.ROLLED_BACK,
            OtaAttemptState.REJECTED,
            OtaAttemptState.ABORTED,
        ):
            for tgt in OtaAttemptState:
                if tgt == terminal:
                    continue
                with pytest.raises(OtaStateTransitionError):
                    assert_transition(terminal, tgt)


class TestMonotonicVersioning:
    def test_strict_upgrade_accepted(self) -> None:
        orch = OtaOrchestrator(revocation_list=_fresh_revocation_list())
        attempt = orch.queue_attempt(
            node_installation_id=uuid4(),
            target_image_version="1.1.0",
            target_image_digest="sha256:abc",
            from_image_version="1.0.0",
        )
        assert attempt.state == OtaAttemptState.QUEUED
        assert attempt.rejection_reason is None

    def test_downgrade_blocked_without_flag(self) -> None:
        orch = OtaOrchestrator(revocation_list=_fresh_revocation_list())
        attempt = orch.queue_attempt(
            node_installation_id=uuid4(),
            target_image_version="1.0.0",
            target_image_digest="sha256:abc",
            from_image_version="1.1.0",
        )
        assert attempt.state == OtaAttemptState.REJECTED
        assert attempt.rejection_reason == OtaRejectionReason.DOWNGRADE_BLOCKED

    def test_same_version_blocked(self) -> None:
        orch = OtaOrchestrator(revocation_list=_fresh_revocation_list())
        attempt = orch.queue_attempt(
            node_installation_id=uuid4(),
            target_image_version="1.0.0",
            target_image_digest="sha256:abc",
            from_image_version="1.0.0",
        )
        assert attempt.rejection_reason == OtaRejectionReason.DOWNGRADE_BLOCKED

    def test_downgrade_with_allow_flag(self) -> None:
        orch = OtaOrchestrator(revocation_list=_fresh_revocation_list())
        attempt = orch.queue_attempt(
            node_installation_id=uuid4(),
            target_image_version="1.0.0",
            target_image_digest="sha256:abc",
            from_image_version="1.1.0",
            allow_downgrade=True,
        )
        # No rejection (admin tomó la decisión explícita).
        assert attempt.state == OtaAttemptState.QUEUED


class TestRevocationCache:
    def test_revoked_version_rejected(self) -> None:
        orch = OtaOrchestrator(
            revocation_list=_fresh_revocation_list(revoked=("1.0.5", "1.1.0"))
        )
        attempt = orch.queue_attempt(
            node_installation_id=uuid4(),
            target_image_version="1.0.5",
            target_image_digest="sha256:abc",
            from_image_version="1.0.0",
        )
        assert attempt.rejection_reason == OtaRejectionReason.IMAGE_REVOKED

    def test_stale_list_blocks_updates(self) -> None:
        orch = OtaOrchestrator(
            revocation_list=_fresh_revocation_list(age_days=40)  # > 30d TTL
        )
        attempt = orch.queue_attempt(
            node_installation_id=uuid4(),
            target_image_version="1.1.0",
            target_image_digest="sha256:abc",
            from_image_version="1.0.0",
        )
        assert attempt.rejection_reason == OtaRejectionReason.REVOCATION_LIST_STALE

    def test_no_revocation_list_fails_closed(self) -> None:
        orch = OtaOrchestrator(revocation_list=None)
        attempt = orch.queue_attempt(
            node_installation_id=uuid4(),
            target_image_version="1.1.0",
            target_image_digest="sha256:abc",
            from_image_version="1.0.0",
        )
        # Constitución IV: fail-closed sin lista.
        assert attempt.rejection_reason == OtaRejectionReason.REVOCATION_LIST_STALE


class TestTransitions:
    def test_happy_path_to_promoted(self) -> None:
        orch = OtaOrchestrator(revocation_list=_fresh_revocation_list())
        attempt = orch.queue_attempt(
            node_installation_id=uuid4(),
            target_image_version="1.1.0",
            target_image_digest="sha256:abc",
            from_image_version="1.0.0",
        )
        for state in (
            OtaAttemptState.DOWNLOADING,
            OtaAttemptState.VERIFYING,
            OtaAttemptState.DRAIN_IN_PROGRESS,
            OtaAttemptState.STAGED,
            OtaAttemptState.BOOTING_TARGET,
            OtaAttemptState.PROMOTED,
        ):
            orch.transition(attempt, state)
        assert attempt.state == OtaAttemptState.PROMOTED
        assert attempt.concluded_at is not None
        assert attempt.staged_at is not None
        assert attempt.verified_at is not None

    def test_rollback_path(self) -> None:
        orch = OtaOrchestrator(revocation_list=_fresh_revocation_list())
        attempt = orch.queue_attempt(
            node_installation_id=uuid4(),
            target_image_version="1.1.0",
            target_image_digest="sha256:abc",
            from_image_version="1.0.0",
        )
        for state in (
            OtaAttemptState.DOWNLOADING,
            OtaAttemptState.VERIFYING,
            OtaAttemptState.DRAIN_IN_PROGRESS,
            OtaAttemptState.STAGED,
            OtaAttemptState.BOOTING_TARGET,
        ):
            orch.transition(attempt, state)
        orch.transition(
            attempt,
            OtaAttemptState.ROLLED_BACK,
            rollback_reason=OtaRollbackReason.HEALTHY_TARGET_TIMEOUT,
        )
        assert attempt.rollback_reason == OtaRollbackReason.HEALTHY_TARGET_TIMEOUT


class TestRevocationListStaleness:
    def test_fresh_list_not_stale(self) -> None:
        rl = _fresh_revocation_list(age_days=5)
        assert not rl.is_stale(now=datetime.now(tz=UTC))

    def test_stale_after_ttl(self) -> None:
        rl = _fresh_revocation_list(age_days=40)
        assert rl.is_stale(now=datetime.now(tz=UTC))
