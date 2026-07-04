"""End-to-end: Enterprise-routed native-danger approval (Fase 2 Phase 4b).

Extends test_hitl_block_and_resume.py's block-and-resume coverage with the
ENTERPRISE route: `_resolve_native_danger_approval(route=ApprovalRoute.
ENTERPRISE, ...)` persists route='enterprise' + sensitivity + agent_id on the
pending row, and the SAME threading.Event resume seam applies regardless of
who signals it (local D-Bus deny, or — in production — a verified cloud
DecisionEnvelope applied by hermes.config_sync.remote_approvals).

Covers the invariants pinned by the design:
  I-1 the caged agent cannot self-approve: covered structurally (this test
      never mints an approval without either a local reject or a real
      cryptographic verify — see test_remote_approvals.py for the envelope
      matrix).
  I-2 local DENY on an ENTERPRISE-routed row still resumes with "denied".
  I-3 local APPROVE on an ENTERPRISE-routed row is rejected fail-closed by
      SqliteApprovalGate.approve() — it never mints a token for that row.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


def _make_broker_and_loop(db_path: Path) -> tuple:
    from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner  # noqa: F401
    from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
    from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate

    signing_key = b"test-signing-key-32-bytes-padded!"[:32]
    minter = HitlApprovalMinter(signing_key=signing_key)
    signer = MagicMock()
    signer.append = MagicMock()
    signer.append_and_persist = AsyncMock()

    gate = SqliteApprovalGate(
        db_path=db_path, minter=minter, signer=signer, audit_repo=None, mfa_verifier=None,
    )
    broker = MagicMock()
    broker._approval_gate = gate

    loop = asyncio.new_event_loop()
    return broker, loop, gate


def _row_for(db_path: Path, proposal_id_str: str) -> sqlite3.Row:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM pending_approvals WHERE proposal_id = ?", (proposal_id_str,)
    ).fetchone()
    conn.close()
    return row


def _wait_for_pending_slot(timeout_s: float = 10.0) -> str:
    from hermes.runtime.security_hook import _pending_events, _pending_events_lock

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with _pending_events_lock:
            if _pending_events:
                return next(iter(_pending_events))
        time.sleep(0.05)
    raise AssertionError("Hook never registered a pending slot")


class TestEnterpriseRouteRegistersPendingRow:
    def test_register_persists_route_sensitivity_agent_id(self, tmp_path: Path) -> None:
        from hermes.capabilities.approval_router import ApprovalRoute
        from hermes.capabilities.tool_sensitivity import SensitivityCategory
        from hermes.runtime.security_hook import (
            _resolve_native_danger_approval,
            signal_native_danger_approval,
        )

        db_path = tmp_path / "test.db"
        broker, loop, _gate = _make_broker_and_loop(db_path)
        result_holder: list = []

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        loop_thread = threading.Thread(target=_run_loop, daemon=True)
        loop_thread.start()

        def _hook_thread() -> None:
            result = _resolve_native_danger_approval(
                "cronjob",
                {"schedule": "0 9 * * *"},
                broker,
                loop,
                conversation_id="conv-enterprise-1",
                route=ApprovalRoute.ENTERPRISE,
                sensitivity_categories=frozenset({SensitivityCategory.PII_READ}),
                agent_id="sales-bot",
            )
            result_holder.append(result)

        hook = threading.Thread(target=_hook_thread, daemon=True)
        hook.start()

        proposal_id_str = _wait_for_pending_slot()

        row = _row_for(db_path, proposal_id_str)
        assert row["route"] == "enterprise"
        assert json.loads(row["sensitivity"]) == ["pii_read"]
        assert row["agent_id"] == "sales-bot"
        assert row["status"] == "pending"

        signal_native_danger_approval(proposal_id_str, "approved")
        hook.join(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)

        assert result_holder[0] is None  # ALLOW — the exact same call resumes


class TestLocalDenyAlwaysWorksOnEnterpriseRoute:
    def test_local_deny_resumes_with_denied_even_when_enterprise_routed(
        self, tmp_path: Path
    ) -> None:
        """I-2: the local human can ALWAYS deny, regardless of route."""
        from hermes.capabilities.approval_router import ApprovalRoute
        from hermes.runtime.security_hook import (
            _resolve_native_danger_approval,
            signal_native_danger_approval,
        )

        db_path = tmp_path / "test.db"
        broker, loop, _gate = _make_broker_and_loop(db_path)
        result_holder: list = []

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        loop_thread = threading.Thread(target=_run_loop, daemon=True)
        loop_thread.start()

        def _hook_thread() -> None:
            result = _resolve_native_danger_approval(
                "skill_manage",
                {"action": "install"},
                broker,
                loop,
                conversation_id="conv-enterprise-2",
                route=ApprovalRoute.ENTERPRISE,
            )
            result_holder.append(result)

        hook = threading.Thread(target=_hook_thread, daemon=True)
        hook.start()

        proposal_id_str = _wait_for_pending_slot()

        # Simulate the local D-Bus reject_action path — deny always resumes,
        # even though this row is routed to Enterprise.
        signalled = signal_native_danger_approval(proposal_id_str, "denied")

        hook.join(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)

        assert signalled is True
        assert result_holder[0] is not None
        assert "rechazó" in result_holder[0] or "denied" in result_holder[0].lower()


class TestLocalApproveRejectedOnEnterpriseRoute:
    @pytest.mark.asyncio
    async def test_gate_approve_raises_for_enterprise_routed_row(
        self, tmp_path: Path
    ) -> None:
        """I-1/I-3: the gate is the single choke-point — a LOCAL approve() call
        against an 'enterprise' route MUST fail closed (no token minted), no
        matter which surface calls it (D-Bus approve_action, tests, etc.)."""
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
        from hermes.capabilities.domain.ports import ConsentContext, RiskLevel
        from hermes.capabilities.infrastructure.sqlite_approval_gate import (
            ApprovalGateError,
            SqliteApprovalGate,
        )

        db_path = tmp_path / "gate.db"
        signing_key = b"test-signing-key-32-bytes-padded!"[:32]
        signer = MagicMock()
        signer.append = MagicMock()
        signer.append_and_persist = AsyncMock()
        gate = SqliteApprovalGate(
            db_path=db_path,
            minter=HitlApprovalMinter(signing_key=signing_key),
            signer=signer,
            audit_repo=None,
            mfa_verifier=None,
        )

        proposal_id = uuid4()
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=uuid4(),
            consent_context=ConsentContext(tenant_id=uuid4(), operator_id=uuid4()),
            risk=RiskLevel.HIGH,
            justification="enterprise route regression",
            parameters_redacted={"action": "install"},
            tool_name="skill_manage",
            action_digest="dig-enterprise-1",
            route="enterprise",
        )

        with pytest.raises(ApprovalGateError) as exc_info:
            await gate.approve(proposal_id=proposal_id, approved_by=uuid4())

        assert exc_info.value.reason == "enterprise_route_requires_cloud_decision"

        # The row must still be resolvable by reject() (I-2) — untouched by the
        # enterprise-route guard, which ONLY blocks approve().
        await gate.reject(proposal_id=proposal_id, rejected_by=uuid4(), reason="owner denied")
        row = _row_for(db_path, str(proposal_id))
        assert row["status"] == "rejected"
