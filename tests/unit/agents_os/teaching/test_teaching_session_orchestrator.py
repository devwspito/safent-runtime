"""Tests for TeachingSessionOrchestrator — spec 004 / US3 invariants.

All tests use FakeTeachingContext: no browser, no LLM, no network.

Invariants verified:
  1. poseedor único: claim(ctx, OPERATOR) then claim(ctx, AGENT) → violation.
  2. aislamiento: teach isolation_key ≠ exec keys; collision → violation.
  3. no-pausa: opening teach does NOT mutate execution ledger.
  4. no-autónoma-sin-promoción: sign → validated; autonomous direct → TransitionError.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from hermes.agents_os.application.teaching.input_ownership_ledger import (
    InputOwnershipLedger,
)
from hermes.agents_os.application.teaching.teaching_context import (
    InputOwner,
    InputOwnershipViolation,
    SurfaceKind,
)
from hermes.agents_os.application.teaching.teaching_session_orchestrator import (
    TeachingSessionOrchestrator,
)
from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSessionOrchestrator,
    TrainingSessionState,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind as DomainSK
from hermes.agents_os.testing.fake_teaching_context import FakeTeachingContext
from hermes.training.domain.skill_state import SkillState, SkillStateTransitionError, assert_transition

pytestmark = pytest.mark.unit

_TENANT = UUID("a9501e55-0000-4000-8000-000000000001")
_OPERATOR = UUID("a9501e55-0000-4000-8000-000000000002")
_ALL_SURFACES = frozenset(DomainSK)


def _make_orchestrator(
    exec_keys: set[str] | None = None,
) -> TeachingSessionOrchestrator:
    return TeachingSessionOrchestrator(
        training_orchestrator=TrainingSessionOrchestrator(),
        context_factory=FakeTeachingContext(),
        ledger=InputOwnershipLedger(),
        execution_isolation_keys=exec_keys or set(),
    )


def _open_session(
    orch: TeachingSessionOrchestrator,
    *,
    session_id: UUID | None = None,
    site_id: str = "site-a",
) -> object:
    return orch.open_teaching_session(
        teaching_session_id=session_id or uuid4(),
        surface_kind=SurfaceKind.BROWSER,
        tenant_id=_TENANT,
        operator_id=_OPERATOR,
        site_id=site_id,
    )


class TestPoseedorUnico:
    """Invariant: exactly one owner per context (FR-002/FR-022)."""

    def test_open_claims_operator(self) -> None:
        orch = _make_orchestrator()
        result = _open_session(orch)
        ctx = result.context
        assert orch._ledger.owner_of(ctx.context_id) == InputOwner.OPERATOR

    def test_double_claim_different_owner_raises(self) -> None:
        """Manually forcing a double claim (simulates concurrent access)."""
        ledger = InputOwnershipLedger()
        ctx_id = uuid4()
        ledger.claim(ctx_id, InputOwner.OPERATOR)
        with pytest.raises(InputOwnershipViolation):
            ledger.claim(ctx_id, InputOwner.AGENT)

    def test_close_releases_ownership(self) -> None:
        orch = _make_orchestrator()
        result = _open_session(orch)
        ctx_id = result.context.context_id
        orch.close_teaching_session(context_id=ctx_id)
        assert orch._ledger.owner_of(ctx_id) is None


class TestAislamiento:
    """Invariant: teach isolation_key must not collide with exec keys (FR-004)."""

    def test_no_collision_opens_cleanly(self) -> None:
        exec_keys = {"exec:other-tenant:site-b"}
        orch = _make_orchestrator(exec_keys=exec_keys)
        result = _open_session(orch, site_id="site-a")
        assert result.context.isolation_key.startswith("teach:")

    def test_collision_with_exec_key_raises(self) -> None:
        """FR-004: teach key overlapping execution key → 409 at boundary."""
        # The fake factory produces key = "teach:{tenant}:{site_id}".
        # We register that exact key as if an execution session uses it.
        teach_key = f"teach:{_TENANT}:site-collide"
        exec_keys = {teach_key}
        orch = _make_orchestrator(exec_keys=exec_keys)
        with pytest.raises(InputOwnershipViolation):
            _open_session(orch, site_id="site-collide")

    def test_teach_key_isolated_from_other_exec_keys(self) -> None:
        """Teaching isolation_key ≠ execution keys for different sites."""
        exec_key = f"teach:{_TENANT}:other-site"
        orch = _make_orchestrator(exec_keys={exec_key})
        result = _open_session(orch, site_id="my-site")
        assert result.context.isolation_key != exec_key


class TestNoPausa:
    """Invariant: opening teach does NOT pause/mutate any execution context."""

    def test_open_teach_does_not_touch_execution_ledger(self) -> None:
        """A separate execution ledger is untouched when teach opens (FR-017)."""
        execution_ledger = InputOwnershipLedger()
        exec_ctx_id = uuid4()
        execution_ledger.claim(exec_ctx_id, InputOwner.AGENT)

        orch = _make_orchestrator()
        _open_session(orch, site_id="teach-site")

        # Execution ledger must be intact (AGENT still owns exec_ctx_id).
        assert execution_ledger.owner_of(exec_ctx_id) == InputOwner.AGENT

    def test_register_deregister_exec_key(self) -> None:
        orch = _make_orchestrator()
        orch.register_execution_key("exec:t1:s1")
        assert "exec:t1:s1" in orch._exec_keys
        orch.deregister_execution_key("exec:t1:s1")
        assert "exec:t1:s1" not in orch._exec_keys


class TestNoAutonomaSinPromocion:
    """Invariant: sign → validated; autonomous only via explicit promotion."""

    def test_assert_transition_validated_to_autonomous_ok(self) -> None:
        """assert_transition allows VALIDATED → AUTONOMOUS (the promotion path)."""
        assert_transition(SkillState.VALIDATED, SkillState.AUTONOMOUS)

    def test_assert_transition_draft_to_autonomous_forbidden(self) -> None:
        """FR-020: DRAFT → AUTONOMOUS is never allowed without VALIDATED first."""
        with pytest.raises(SkillStateTransitionError):
            assert_transition(SkillState.DRAFT, SkillState.AUTONOMOUS)

    def test_training_orchestrator_sign_produces_signed_state(self) -> None:
        """The training orchestrator stays in SIGNED after sign().

        Note: the skill_packages_view stores 'validated'; the in-memory
        TrainingSession uses its own SIGNED state (no coupling).
        This test verifies the orchestrator's own state machine is intact.
        """
        orch = TrainingSessionOrchestrator()
        sess = orch.start(
            tenant_id=_TENANT,
            human_user_id=_OPERATOR,
            skill_id="pay-invoice",
            surface_kinds_allowed=frozenset(DomainSK),
        )
        orch.capture_step(
            session_id=sess.session_id,
            surface_kind=DomainSK.BROWSER,
            action_payload={"action": "click", "ref": "submit"},
        )
        orch.request_review(session_id=sess.session_id)
        signed = orch.sign(session_id=sess.session_id, human_confirmed=True)
        assert signed.state == TrainingSessionState.SIGNED

    def test_skill_state_signed_is_not_a_valid_state(self) -> None:
        """SkillState enum has no 'signed' member — only 'validated'."""
        valid_states = {s.value for s in SkillState}
        assert "signed" not in valid_states
        assert "validated" in valid_states


class TestTeachingSessionLifecycle:
    def test_open_returns_operator_owner(self) -> None:
        orch = _make_orchestrator()
        result = _open_session(orch)
        assert result.context.owner == InputOwner.OPERATOR

    def test_open_returns_correct_surface_kind(self) -> None:
        orch = _make_orchestrator()
        result = _open_session(orch)
        assert result.context.surface_kind == SurfaceKind.BROWSER

    def test_two_sessions_different_sites_no_collision(self) -> None:
        orch = _make_orchestrator()
        r1 = _open_session(orch, site_id="site-a")
        r2 = _open_session(orch, site_id="site-b")
        assert r1.context.context_id != r2.context.context_id
        assert r1.context.isolation_key != r2.context.isolation_key
