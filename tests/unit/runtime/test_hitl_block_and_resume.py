"""Regression test: HITL native-danger block-and-resume (Mandato 1, 2026-06-25).

Bug: approve HITL → 200 OK but action never executed.

Root cause: _resolve_native_danger_approval registered a pending DB row and returned
a block message immediately — the conversation thread was never paused. approve_action
minted a token but there was no thread waiting to be unblocked. The "chat continuation"
re-prompt workaround caused the LLM to regenerate different args → digest mismatch →
infinite re-approval loop.

Fix: _resolve_native_danger_approval now registers a threading.Event slot in
_pending_events under the proposal_id, blocks the conversation thread on event.wait(),
and returns None (ALLOW) when signal_native_danger_approval("approved") fires the event.
approve_action / reject_action signal the event via signal_native_danger_approval so
the SAME blocked tool call resumes — no re-prompt, no digest drift.

Mandato 2: per-action approvals require no MFA; sqlite_approval_gate.approve no longer
calls mfa_verifier for per-action proposals.

These tests verify:
1. signal_native_danger_approval signals the correct event and returns True.
2. A second call for the same proposal_id (after cleanup) returns False.
3. _resolve_native_danger_approval returns None (ALLOW) when signalled "approved".
4. _resolve_native_danger_approval returns a block message when signalled "denied".
5. _resolve_native_danger_approval returns a timeout block message on timeout.
6. sqlite_approval_gate.approve succeeds without mfa_verifier (no ApprovalGateError).
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest


# ---------------------------------------------------------------------------
# signal_native_danger_approval unit tests
# ---------------------------------------------------------------------------

class TestSignalNativeDangerApproval:
    """signal_native_danger_approval: signalling mechanics and cleanup."""

    def _inject_slot(self, proposal_id: str) -> dict:
        """Inject a slot via the public registry helper for testing."""
        from hermes.runtime.security_hook import _register_pending_event

        event = threading.Event()
        slot = {"event": event, "choice": None}
        _register_pending_event(proposal_id, slot)
        return slot

    def _remove_slot(self, proposal_id: str) -> None:
        from hermes.runtime.security_hook import _pending_events, _pending_events_lock

        with _pending_events_lock:
            _pending_events.pop(proposal_id, None)

    def test_signals_waiting_thread_returns_true(self) -> None:
        """signal_native_danger_approval returns True when a slot exists."""
        from hermes.runtime.security_hook import signal_native_danger_approval

        pid = str(uuid4())
        slot = self._inject_slot(pid)
        try:
            result = signal_native_danger_approval(pid, "approved")
            assert result is True
            assert slot["event"].is_set()
            assert slot["choice"] == "approved"
        finally:
            self._remove_slot(pid)

    def test_returns_false_when_no_slot(self) -> None:
        """signal_native_danger_approval returns False for an unknown proposal_id."""
        from hermes.runtime.security_hook import signal_native_danger_approval

        result = signal_native_danger_approval(str(uuid4()), "approved")
        assert result is False

    def test_denied_choice_propagates(self) -> None:
        """signal_native_danger_approval sets choice='denied' correctly."""
        from hermes.runtime.security_hook import signal_native_danger_approval

        pid = str(uuid4())
        slot = self._inject_slot(pid)
        try:
            signal_native_danger_approval(pid, "denied")
            assert slot["choice"] == "denied"
        finally:
            self._remove_slot(pid)

    def test_returns_false_after_slot_cleaned_up(self) -> None:
        """After the hook removes its slot, signal returns False (no double-signal)."""
        from hermes.runtime.security_hook import signal_native_danger_approval

        pid = str(uuid4())
        self._inject_slot(pid)
        self._remove_slot(pid)  # simulate hook timeout cleanup
        result = signal_native_danger_approval(pid, "approved")
        assert result is False


# ---------------------------------------------------------------------------
# _resolve_native_danger_approval integration tests (with fake gate/loop)
# ---------------------------------------------------------------------------

class TestResolveNativeDangerApproval:
    """_resolve_native_danger_approval: blocking, signalling, timeout, deny."""

    def _make_broker_and_loop(self, db_path: Path) -> tuple:
        """Build a minimal fake broker with a real SqliteApprovalGate and asyncio loop."""
        from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner, AuditKind  # noqa: F401

        signing_key = b"test-signing-key-32-bytes-padded!"[:32]
        minter = HitlApprovalMinter(signing_key=signing_key)
        signer = MagicMock()
        signer.append = MagicMock()
        signer.append_and_persist = AsyncMock()

        gate = SqliteApprovalGate(
            db_path=db_path,
            minter=minter,
            signer=signer,
            audit_repo=None,
            mfa_verifier=None,  # Mandato 2: no MFA for per-action
        )

        broker = MagicMock()
        broker._approval_gate = gate

        loop = asyncio.new_event_loop()
        return broker, loop, gate

    def test_blocks_and_resumes_approved(self, tmp_path: Path) -> None:
        """The hook blocks until signalled 'approved', then returns None (ALLOW)."""
        from hermes.runtime.security_hook import (
            _resolve_native_danger_approval,
            signal_native_danger_approval,
            _pending_events,
            _pending_events_lock,
        )

        broker, loop, _gate = self._make_broker_and_loop(tmp_path / "test.db")
        result_holder: list = []
        ready = threading.Event()

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        loop_thread = threading.Thread(target=_run_loop, daemon=True)
        loop_thread.start()

        def _hook_thread() -> None:
            ready.set()
            result = _resolve_native_danger_approval(
                "cronjob",
                {"action": "create", "schedule": "0 9 * * *"},
                broker,
                loop,
                conversation_id="conv-test-1",
            )
            result_holder.append(result)

        hook = threading.Thread(target=_hook_thread, daemon=True)
        hook.start()

        # Wait for hook to start and register its slot.
        ready.wait(timeout=5)

        # Poll until the slot appears (hook registers it after gate.register_pending).
        import time
        deadline = time.monotonic() + 10
        proposal_id_str = None
        while time.monotonic() < deadline:
            with _pending_events_lock:
                if _pending_events:
                    proposal_id_str = next(iter(_pending_events))
                    break
            time.sleep(0.05)

        assert proposal_id_str is not None, "Hook never registered a pending slot"

        # Signal approved.
        signal_native_danger_approval(proposal_id_str, "approved")

        hook.join(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)

        assert result_holder, "Hook thread did not complete"
        assert result_holder[0] is None, (
            f"Expected None (ALLOW) on approved, got: {result_holder[0]!r}"
        )

    def test_blocks_and_resumes_denied(self, tmp_path: Path) -> None:
        """The hook returns a block message when signalled 'denied'."""
        from hermes.runtime.security_hook import (
            _resolve_native_danger_approval,
            signal_native_danger_approval,
            _pending_events,
            _pending_events_lock,
        )

        broker, loop, _gate = self._make_broker_and_loop(tmp_path / "test.db")
        result_holder: list = []
        ready = threading.Event()

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        loop_thread = threading.Thread(target=_run_loop, daemon=True)
        loop_thread.start()

        def _hook_thread() -> None:
            ready.set()
            result = _resolve_native_danger_approval(
                "send_message",
                {"to": "someone@example.com", "body": "hello"},
                broker,
                loop,
                conversation_id="conv-test-2",
            )
            result_holder.append(result)

        hook = threading.Thread(target=_hook_thread, daemon=True)
        hook.start()

        ready.wait(timeout=5)

        import time
        deadline = time.monotonic() + 10
        proposal_id_str = None
        while time.monotonic() < deadline:
            with _pending_events_lock:
                if _pending_events:
                    proposal_id_str = next(iter(_pending_events))
                    break
            time.sleep(0.05)

        assert proposal_id_str is not None

        signal_native_danger_approval(proposal_id_str, "denied")

        hook.join(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)

        assert result_holder
        assert result_holder[0] is not None, "Expected block message on denied, got None"
        assert "rechazó" in result_holder[0] or "denied" in result_holder[0].lower()

    def test_timeout_returns_block_message(self, tmp_path: Path) -> None:
        """The hook returns a block message when the owner doesn't respond (timeout)."""
        from hermes.runtime.security_hook import _resolve_native_danger_approval

        broker, loop, _gate = self._make_broker_and_loop(tmp_path / "test.db")
        result_holder: list = []

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        loop_thread = threading.Thread(target=_run_loop, daemon=True)
        loop_thread.start()

        # Override the wait timeout to a tiny value for this test.
        import hermes.runtime.security_hook as _sh
        original_timeout = _sh._NATIVE_DANGER_OWNER_WAIT_S
        _sh._NATIVE_DANGER_OWNER_WAIT_S = 0.2  # 200ms

        try:
            result = _resolve_native_danger_approval(
                "delegate_task",
                {"task": "do something"},
                broker,
                loop,
                conversation_id="conv-test-3",
            )
            result_holder.append(result)
        finally:
            _sh._NATIVE_DANGER_OWNER_WAIT_S = original_timeout
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=5)

        assert result_holder
        assert result_holder[0] is not None, "Expected block message on timeout, got None"
        assert "tiempo" in result_holder[0].lower() or "timeout" in result_holder[0].lower()


# ---------------------------------------------------------------------------
# sqlite_approval_gate.approve: no MFA required (Mandato 2)
# ---------------------------------------------------------------------------

class TestApproveGateNoMfa:
    """gate.approve succeeds without mfa_verifier — Mandato 2 regression test.

    Before the fix: gate.approve raised ApprovalGateError('MFA verifier no configurado')
    when mfa_verifier=None, blocking ALL per-action approvals.
    After the fix: gate.approve proceeds directly to mint the token.
    """

    @pytest.mark.asyncio
    async def test_approve_without_mfa_verifier_succeeds(self, tmp_path: Path) -> None:
        """gate.approve(mfa_verifier=None) returns a token (not raises)."""
        from hermes.capabilities.infrastructure.sqlite_approval_gate import (
            SqliteApprovalGate,
            ApprovalGateError,
        )
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
        from hermes.capabilities.domain.ports import ConsentContext, RiskLevel

        signing_key = b"test-key-must-be-32-bytes-padded"[:32]
        minter = HitlApprovalMinter(signing_key=signing_key)
        signer = MagicMock()
        signer.append = MagicMock()
        signer.append_and_persist = AsyncMock()

        gate = SqliteApprovalGate(
            db_path=tmp_path / "gate.db",
            minter=minter,
            signer=signer,
            audit_repo=None,
            mfa_verifier=None,  # no MFA verifier
        )

        proposal_id = uuid4()
        operator_id = uuid4()

        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=uuid4(),
            consent_context=ConsentContext(
                operator_id=operator_id, tenant_id=uuid4()
            ),
            risk=RiskLevel.HIGH,
            justification="test — per-action approval without MFA",
            parameters_redacted={"action": "create", "schedule": "0 9 * * *"},
            tool_name="cronjob",
            action_digest="abc123",
        )

        # Before fix: this raised ApprovalGateError due to mfa_verifier=None
        # After fix: returns a token string
        try:
            token = await gate.approve(
                proposal_id=proposal_id,
                approved_by=operator_id,
                mfa_factors=None,
            )
        except ApprovalGateError as exc:
            pytest.fail(
                f"gate.approve raised ApprovalGateError (MFA gate not removed): {exc}"
            )

        assert isinstance(token, str) and len(token) > 0, (
            f"Expected a non-empty token, got: {token!r}"
        )

    @pytest.mark.asyncio
    async def test_approve_proposal_not_found_raises(self, tmp_path: Path) -> None:
        """gate.approve raises ApprovalGateError for unknown proposal (fail-closed)."""
        from hermes.capabilities.infrastructure.sqlite_approval_gate import (
            SqliteApprovalGate,
            ApprovalGateError,
        )
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter

        signing_key = b"test-key-must-be-32-bytes-padded"[:32]
        minter = HitlApprovalMinter(signing_key=signing_key)
        signer = MagicMock()
        signer.append = MagicMock()
        signer.append_and_persist = AsyncMock()

        gate = SqliteApprovalGate(
            db_path=tmp_path / "gate2.db",
            minter=minter,
            signer=signer,
            audit_repo=None,
            mfa_verifier=None,
        )

        with pytest.raises(ApprovalGateError, match="no existe o ya fue resuelta"):
            await gate.approve(
                proposal_id=uuid4(),
                approved_by=uuid4(),
            )


# ---------------------------------------------------------------------------
# Escalated MFA model: is_mfa_required / tier classification
# ---------------------------------------------------------------------------

class TestEscalatedMfaTier:
    """Regression: is_mfa_required classifies tools to simple vs mfa tier.

    Owner decision 2026-06-25: cronjob is SIMPLE (approved without TOTP).
    install_*, set_policy, disable_mfa, skill_manage are MFA tier.
    Classification must come from tool_delicacy, never word-list scanning.
    """

    def test_cronjob_is_simple_tier(self) -> None:
        """cronjob is explicitly simple tier — no TOTP needed to approve."""
        from hermes.capabilities.tool_delicacy import is_mfa_required
        assert is_mfa_required("cronjob") is False, (
            "cronjob must be simple tier (owner approved no TOTP, 2026-06-25)"
        )

    def test_install_app_is_mfa_tier(self) -> None:
        """install_app is mfa tier — TOTP required to approve."""
        from hermes.capabilities.tool_delicacy import is_mfa_required
        assert is_mfa_required("install_app") is True

    def test_install_mcp_is_mfa_tier(self) -> None:
        from hermes.capabilities.tool_delicacy import is_mfa_required
        assert is_mfa_required("install_mcp") is True

    def test_install_skill_is_mfa_tier(self) -> None:
        from hermes.capabilities.tool_delicacy import is_mfa_required
        assert is_mfa_required("install_skill") is True

    def test_skill_manage_is_mfa_tier(self) -> None:
        from hermes.capabilities.tool_delicacy import is_mfa_required
        assert is_mfa_required("skill_manage") is True

    def test_set_policy_is_mfa_tier(self) -> None:
        from hermes.capabilities.tool_delicacy import is_mfa_required
        assert is_mfa_required("set_policy") is True

    def test_disable_mfa_is_mfa_tier(self) -> None:
        from hermes.capabilities.tool_delicacy import is_mfa_required
        assert is_mfa_required("disable_mfa") is True

    def test_send_message_is_simple_tier(self) -> None:
        """send_message is simple tier — no TOTP needed."""
        from hermes.capabilities.tool_delicacy import is_mfa_required
        assert is_mfa_required("send_message") is False

    def test_delegate_task_is_simple_tier(self) -> None:
        from hermes.capabilities.tool_delicacy import is_mfa_required
        assert is_mfa_required("delegate_task") is False

    def test_unknown_tool_is_simple_tier(self) -> None:
        """Unknown tools default to simple tier (fail-open for approval, cage confines)."""
        from hermes.capabilities.tool_delicacy import is_mfa_required
        assert is_mfa_required("some_future_tool") is False

    @pytest.mark.asyncio
    async def test_gate_approve_simple_tier_succeeds_without_mfa_verifier(
        self, tmp_path: Path
    ) -> None:
        """simple-tier proposal: gate.approve succeeds with mfa_verifier=None and mfa_factors=None."""
        from hermes.capabilities.infrastructure.sqlite_approval_gate import (
            SqliteApprovalGate,
            ApprovalGateError,
        )
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
        from hermes.capabilities.domain.ports import ConsentContext, RiskLevel

        signing_key = b"test-key-must-be-32-bytes-padded"[:32]
        minter = HitlApprovalMinter(signing_key=signing_key)
        signer = MagicMock()
        signer.append = MagicMock()
        signer.append_and_persist = AsyncMock()

        gate = SqliteApprovalGate(
            db_path=tmp_path / "gate_simple.db",
            minter=minter,
            signer=signer,
            audit_repo=None,
            mfa_verifier=None,
        )
        proposal_id = uuid4()
        operator_id = uuid4()
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=uuid4(),
            consent_context=ConsentContext(operator_id=operator_id, tenant_id=uuid4()),
            risk=RiskLevel.HIGH,
            justification="cronjob — simple tier, no MFA",
            parameters_redacted={"schedule": "0 9 * * *"},
            tool_name="cronjob",
            action_digest="abc123",
        )
        try:
            token = await gate.approve(proposal_id=proposal_id, approved_by=operator_id, mfa_factors=None)
        except ApprovalGateError as exc:
            pytest.fail(f"simple-tier approve should NOT require MFA but raised: {exc}")
        assert token and len(token) > 0

    @pytest.mark.asyncio
    async def test_gate_approve_mfa_tier_requires_verifier(self, tmp_path: Path) -> None:
        """mfa-tier proposal: gate.approve raises ApprovalGateError when mfa_verifier=None."""
        from hermes.capabilities.infrastructure.sqlite_approval_gate import (
            SqliteApprovalGate,
            ApprovalGateError,
        )
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
        from hermes.capabilities.domain.ports import ConsentContext, RiskLevel

        signing_key = b"test-key-must-be-32-bytes-padded"[:32]
        minter = HitlApprovalMinter(signing_key=signing_key)
        signer = MagicMock()
        signer.append = MagicMock()
        signer.append_and_persist = AsyncMock()

        gate = SqliteApprovalGate(
            db_path=tmp_path / "gate_mfa.db",
            minter=minter,
            signer=signer,
            audit_repo=None,
            mfa_verifier=None,  # no verifier → should FAIL for mfa-tier tools
        )
        proposal_id = uuid4()
        operator_id = uuid4()
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=uuid4(),
            consent_context=ConsentContext(operator_id=operator_id, tenant_id=uuid4()),
            risk=RiskLevel.HIGH,
            justification="install_skill — mfa tier, TOTP required",
            parameters_redacted={"skill_id": "some-skill"},
            tool_name="install_skill",
            action_digest="def456",
        )
        with pytest.raises(ApprovalGateError, match="mfa-tier"):
            await gate.approve(proposal_id=proposal_id, approved_by=operator_id, mfa_factors=None)
