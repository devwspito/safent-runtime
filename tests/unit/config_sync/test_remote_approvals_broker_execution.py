"""Cloud-approval -> broker EXECUTION path (Fase 2 Phase 4e — Part B).

The prior gap: a broker route='enterprise' row, once cloud-approved, never
executed — `_verify_and_apply_decision` only flipped status + signalled the
NATIVE Event (a no-op for a broker row, which has no blocked conversation
thread waiting on it). This module proves the FULL, HONEST end-to-end chain
with the REAL infrastructure (SQLite-backed `SqliteApprovalGate` +
`SqliteWorkQueue` + `CapabilityBroker`, no mocking of the mechanics under
test):

  1. `CapabilityBroker.dispatch()` registers a route='enterprise' pending row
     for an MFA-tier tool, with the REAL WorkQueue work_item_id attached
     (PENDING_APPROVAL, work item transitions to 'pending_approval').
  2. A VERIFIED (Ed25519) cloud "approve" decision applied via
     `_verify_and_apply_decision` mints the token_hmac
     (`approve_enterprise_decision`) AND re-enqueues the work item
     ('pending_approval' -> 'pending') — mirroring what
     `dbus_runtime_service.approve_action` does for a LOCAL broker approval.
  3. Re-dispatching the SAME proposal with the minted token EXECUTES it (the
     adapter's `replay` actually runs — not just a status flip).

Also covers the required negative cases: cloud DENY (blocked, no token, not
executed), a LOCAL worker approve() attempt on the enterprise row (still
fails closed), and a tampered signature (rejected, no mint, no re-enqueue).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
from hermes.capabilities.application.capability_broker import CapabilityBroker
from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
from hermes.capabilities.application.intent_log import IntentLog
from hermes.capabilities.domain.agent_access_scope import AgentAccessScope
from hermes.capabilities.domain.ports import (
    CapabilityBinding,
    ConsentContext,
    ExecutionStatus,
    RiskLevel,
)
from hermes.capabilities.infrastructure.sqlite_approval_gate import (
    ApprovalGateError,
    SqliteApprovalGate,
)
from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
    SurfaceAdapterDispatcher,
)
from hermes.config_sync import remote_approvals as ra
from hermes.domain.proposal import ToolCallProposal
from hermes.tasks.domain.ports import WorkItem, WorkItemKind
from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue

pytestmark = pytest.mark.unit

_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()
_OWN_INSTANCE_ID = "instance-broker-exec"
_SIGNING_KEY_HEX = (b"k" * 32).hex()
_ENTERPRISE_ROUTE_MODULE = "hermes.capabilities.infrastructure.enterprise_approval_routing"


class _RecordingAdapter:
    """Minimal FILESYSTEM surface adapter fake — records replayed actions."""

    def __init__(self) -> None:
        self.calls: list[CapturedAction] = []

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.FILESYSTEM

    async def capture(self, **_: object) -> CapturedAction:  # pragma: no cover
        raise NotImplementedError

    async def replay(self, action: CapturedAction, **_: object) -> ReplayOutcome:
        self.calls.append(action)
        return ReplayOutcome(action_id=action.action_id, status=ReplayStatus.EXECUTED_OK)

    def serialize_for_signing(self, action: CapturedAction) -> bytes:  # pragma: no cover
        return b""


class _AllowAllConsent:
    def assert_active(self, *, human_operator_id: object, capability: object) -> object:
        return object()

    def use(self, *, human_operator_id: object, capability: object) -> object:
        return object()


class _FakeCapabilityRegistry:
    def __init__(self, binding: CapabilityBinding) -> None:
        self._binding = binding

    def resolve(self, tool_name: str) -> CapabilityBinding | None:
        return self._binding if tool_name == self._binding.tool_name else None


class _FakeAccessScopeRepo:
    def __init__(self, scope: AgentAccessScope) -> None:
        self._scope = scope

    def get_scope(self, agent_id: str, tenant_id: str) -> AgentAccessScope | None:
        return self._scope


def _cloud_scope() -> AgentAccessScope:
    return AgentAccessScope.create(
        tenant_id=str(_TENANT_ID), agent_id="agent-a", updated_by=1, managed_by="cloud",
    )


def _make_gate(db_path: Path) -> SqliteApprovalGate:
    signer = MagicMock()
    signer.append = MagicMock()
    signer.append_and_persist = AsyncMock()
    return SqliteApprovalGate(
        db_path=db_path,
        minter=HitlApprovalMinter(signing_key=b"k" * 32),
        signer=signer,
        audit_repo=None,
        mfa_verifier=None,
    )


def _make_broker(
    *, db_path: Path, gate: SqliteApprovalGate,
) -> tuple[CapabilityBroker, _RecordingAdapter]:
    binding = CapabilityBinding(
        tool_name="install_app", surface_kind=SurfaceKind.FILESYSTEM,
        required_capability=None, risk=RiskLevel.HIGH, auto_executable=False,
    )
    adapter = _RecordingAdapter()
    dispatcher = SurfaceAdapterDispatcher(adapters={SurfaceKind.FILESYSTEM: adapter})
    audit_repo = SqliteAuditRepository(db_path=db_path)
    broker = CapabilityBroker(
        registry=_FakeCapabilityRegistry(binding),
        consent_manager=_AllowAllConsent(),
        approval_gate=gate,
        dispatcher=dispatcher,
        signer=AuditHashChainSigner(signing_key=b"k" * 32),
        audit_repo=audit_repo,
        intent_log=IntentLog(),
        access_scope_repo=_FakeAccessScopeRepo(_cloud_scope()),
        tenant_id=str(_TENANT_ID),
    )
    return broker, adapter


def _proposal(proposal_id=None) -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=proposal_id or uuid4(),
        tool_name="install_app",
        tenant_id=_TENANT_ID,
        entity_id="test-entity",
        entity_type="test",
        parameters={"op": "install_app"},
        justification="broker enterprise execution test",
    )


def _ctx() -> ConsentContext:
    """Consent carrying the EXPLICIT agent_id (Part A) — this is what routes
    the proposal to ENTERPRISE in the first place."""
    return ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID, agent_id="agent-a")


def _generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key().public_bytes_raw().hex()


def _sign_envelope(private_key: Ed25519PrivateKey, envelope: dict[str, str]) -> str:
    payload = ra.decision_signing_bytes(envelope)
    return private_key.sign(payload).hex()


def _build_envelope(
    *, proposal_id: str, action_digest: str, request_id: str, decision: str = "approve",
) -> dict[str, str]:
    return {
        "action_digest": action_digest,
        "agent_id": "agent-a",
        "approver_user_id": "cloud-admin-1",
        "decided_at": "2026-07-06T12:00:00Z",
        "decision": decision,
        "instance_id": _OWN_INSTANCE_ID,
        "nonce": str(uuid4()),
        "proposal_id": proposal_id,
        "request_id": request_id,
    }


def _seed_push_mapping(db_path: Path, *, proposal_id: str, request_id: str) -> None:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ra._ensure_remote_approval_schema(conn)
    ra._mark_pushed(conn, proposal_id=proposal_id, request_id=request_id, pushed_at="2026-07-06T00:00:00Z")
    conn.commit()
    conn.close()


def _row(db_path: Path, proposal_id: str):
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM pending_approvals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    conn.close()
    return row


async def _register_broker_row(
    *, db_path: Path, broker: CapabilityBroker, queue: SqliteWorkQueue,
) -> tuple[str, "object"]:
    """Dispatches a proposal through the REAL broker + REAL queue — the row
    is registered exactly as it would be in production (route resolved via
    `CapabilityBroker._resolve_enterprise_route`, work_item transitions to
    'pending_approval'). Returns (proposal_id_str, work_item_id)."""
    work_item = WorkItem(
        id=uuid4(), tenant_id=_TENANT_ID, trigger_kind="manual_enqueue",
        kind=WorkItemKind.AUTONOMOUS, payload={"enqueued_by": str(_OPERATOR_ID)},
    )
    await queue.enqueue(work_item)
    claimed = await queue.claim_next()
    assert claimed is not None

    proposal = _proposal()
    with patch(f"{_ENTERPRISE_ROUTE_MODULE}.tenant_remote_approval_enabled", return_value=True):
        outcome = await broker.dispatch(
            proposal, _ctx(), hitl_approval_token=None, work_item_id=claimed.id,
        )
    assert outcome.status == ExecutionStatus.PENDING_APPROVAL

    await queue.mark_pending_approval(
        claimed.id, claim_token=claimed.claim_token, proposal_id=proposal.proposal_id,
    )
    return str(proposal.proposal_id), claimed.id


class TestCloudApprovalExecutesBrokerRow:
    @pytest.mark.asyncio
    async def test_verified_approve_mints_token_reenqueues_and_drain_executes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HERMES_AUDIT_KEY", _SIGNING_KEY_HEX)
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        broker, adapter = _make_broker(db_path=db_path, gate=gate)
        queue = SqliteWorkQueue(db_path=db_path)

        proposal_id_str, work_item_id = await _register_broker_row(
            db_path=db_path, broker=broker, queue=queue,
        )
        row_before = _row(db_path, proposal_id_str)
        assert row_before["route"] == "enterprise"
        assert row_before["agent_id"] == "agent-a"

        private_key, pubkey_hex = _generate_keypair()
        request_id = str(uuid4())
        _seed_push_mapping(db_path, proposal_id=proposal_id_str, request_id=request_id)
        envelope = _build_envelope(
            proposal_id=proposal_id_str, action_digest=row_before["action_digest"] or "",
            request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)

        import sqlite3

        conn = ra._connect(db_path)
        outcome = ra._verify_and_apply_decision(
            item={**envelope, "signature_hex": signature_hex},
            pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID, conn=conn,
            db_path=db_path,
        )
        conn.close()

        assert outcome == "applied"

        row_after = _row(db_path, proposal_id_str)
        assert row_after["status"] == "approved"
        assert row_after["token_hmac"]
        assert row_after["approved_by"] == "enterprise:cloud-decision"

        # Work item re-enqueued: pending_approval -> pending.
        requeued_item = await queue.claim_next()
        assert requeued_item is not None and requeued_item.id == work_item_id

        # The token IS retrievable via approved_token_for (what the loop uses).
        from uuid import UUID

        token = await gate.approved_token_for(UUID(proposal_id_str))
        assert token == row_after["token_hmac"]

        # Re-dispatch the SAME proposal with the approved token -> EXECUTES.
        proposal = _proposal(proposal_id=UUID(proposal_id_str))
        with patch(f"{_ENTERPRISE_ROUTE_MODULE}.tenant_remote_approval_enabled", return_value=True):
            final_outcome = await broker.dispatch(
                proposal, _ctx(), hitl_approval_token=token, work_item_id=work_item_id,
            )
        assert final_outcome.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1, "the adapter must have actually executed, not just flipped status"

    @pytest.mark.asyncio
    async def test_verified_deny_blocks_no_token_not_executed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HERMES_AUDIT_KEY", _SIGNING_KEY_HEX)
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        broker, adapter = _make_broker(db_path=db_path, gate=gate)
        queue = SqliteWorkQueue(db_path=db_path)

        proposal_id_str, work_item_id = await _register_broker_row(
            db_path=db_path, broker=broker, queue=queue,
        )
        row_before = _row(db_path, proposal_id_str)

        private_key, pubkey_hex = _generate_keypair()
        request_id = str(uuid4())
        _seed_push_mapping(db_path, proposal_id=proposal_id_str, request_id=request_id)
        envelope = _build_envelope(
            proposal_id=proposal_id_str, action_digest=row_before["action_digest"] or "",
            request_id=request_id, decision="deny",
        )
        signature_hex = _sign_envelope(private_key, envelope)

        import sqlite3

        conn = ra._connect(db_path)
        with patch("hermes.runtime.security_hook.signal_native_danger_approval") as mock_signal:
            outcome = ra._verify_and_apply_decision(
                item={**envelope, "signature_hex": signature_hex},
                pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID, conn=conn,
                db_path=db_path,
            )
        conn.close()

        assert outcome == "applied"
        mock_signal.assert_called_once_with(proposal_id_str, "denied")

        row_after = _row(db_path, proposal_id_str)
        assert row_after["status"] == "rejected"
        assert row_after["token_hmac"] is None

        from uuid import UUID

        assert await gate.approved_token_for(UUID(proposal_id_str)) is None

        # Work item was NEVER re-enqueued — still stuck in pending_approval.
        assert await queue.claim_next() is None

        # Re-dispatch without a token -> still PENDING_APPROVAL, never executes.
        proposal = _proposal(proposal_id=UUID(proposal_id_str))
        with patch(f"{_ENTERPRISE_ROUTE_MODULE}.tenant_remote_approval_enabled", return_value=True):
            final_outcome = await broker.dispatch(
                proposal, _ctx(), hitl_approval_token=None, work_item_id=work_item_id,
            )
        assert final_outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(adapter.calls) == 0

    @pytest.mark.asyncio
    async def test_local_worker_approve_still_fails_closed_on_broker_enterprise_row(
        self, tmp_path: Path,
    ) -> None:
        """I-1/I-3: the worker has no TOTP for an enterprise row — approve()
        fails closed no matter whether the row came from the native gate or
        the broker."""
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        broker, _adapter = _make_broker(db_path=db_path, gate=gate)
        queue = SqliteWorkQueue(db_path=db_path)

        proposal_id_str, _work_item_id = await _register_broker_row(
            db_path=db_path, broker=broker, queue=queue,
        )

        from uuid import UUID

        with pytest.raises(ApprovalGateError) as exc_info:
            await gate.approve(proposal_id=UUID(proposal_id_str), approved_by=uuid4())
        assert exc_info.value.reason == "enterprise_route_requires_cloud_decision"

        # I-2: the worker can still deny it directly.
        await gate.reject(
            proposal_id=UUID(proposal_id_str), rejected_by=uuid4(), reason="owner denied"
        )
        row = _row(db_path, proposal_id_str)
        assert row["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_tampered_signature_rejected_no_mint_no_reenqueue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HERMES_AUDIT_KEY", _SIGNING_KEY_HEX)
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        broker, _adapter = _make_broker(db_path=db_path, gate=gate)
        queue = SqliteWorkQueue(db_path=db_path)

        proposal_id_str, _work_item_id = await _register_broker_row(
            db_path=db_path, broker=broker, queue=queue,
        )
        row_before = _row(db_path, proposal_id_str)

        private_key, pubkey_hex = _generate_keypair()
        request_id = str(uuid4())
        _seed_push_mapping(db_path, proposal_id=proposal_id_str, request_id=request_id)
        envelope = _build_envelope(
            proposal_id=proposal_id_str, action_digest=row_before["action_digest"] or "",
            request_id=request_id,
        )
        # Tamper the signed envelope AFTER signing — the signature no longer matches.
        tampered = {**envelope, "decision": "approve", "agent_id": "attacker-controlled"}
        signature_hex = _sign_envelope(private_key, envelope)  # signs the ORIGINAL

        import sqlite3

        conn = ra._connect(db_path)
        outcome = ra._verify_and_apply_decision(
            item={**tampered, "signature_hex": signature_hex},
            pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID, conn=conn,
            db_path=db_path,
        )
        conn.close()

        assert outcome == "bad_signature"
        row_after = _row(db_path, proposal_id_str)
        assert row_after["status"] == "pending"
        assert row_after["token_hmac"] is None

        # Work item never touched — still claimable? No: it's 'pending_approval',
        # claim_next() only ever claims 'pending' rows, so it stays put.
        assert await queue.claim_next() is None


class TestTransientMintFailureIsRetryable:
    """Adversarial-review finding (2026-07-06): the anti-replay nonce was
    marked BEFORE the fallible broker mint, so a transient mint failure
    burned the nonce while leaving the row 'pending' and never re-pushed —
    the cloud's next re-serve of the SAME (still valid) decision hit
    'replayed_nonce' (ACKed), permanently dropping a verified approval. This
    class reproduces that exact failure mode and proves it is now retryable.
    """

    @pytest.mark.asyncio
    async def test_transient_mint_failure_then_retry_succeeds_and_executes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HERMES_AUDIT_KEY", _SIGNING_KEY_HEX)
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        broker, adapter = _make_broker(db_path=db_path, gate=gate)
        queue = SqliteWorkQueue(db_path=db_path)

        proposal_id_str, work_item_id = await _register_broker_row(
            db_path=db_path, broker=broker, queue=queue,
        )
        row_before = _row(db_path, proposal_id_str)

        private_key, pubkey_hex = _generate_keypair()
        request_id = str(uuid4())
        _seed_push_mapping(db_path, proposal_id=proposal_id_str, request_id=request_id)
        envelope = _build_envelope(
            proposal_id=proposal_id_str, action_digest=row_before["action_digest"] or "",
            request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)
        item = {**envelope, "signature_hex": signature_hex}

        real_key = bytes.fromhex(_SIGNING_KEY_HEX)
        with patch(
            "hermes.runtime.audit_signing_key.load_signing_key_with_fallback",
            side_effect=[RuntimeError("transient: master.key momentarily unreadable"), real_key],
        ):
            # --- Tick 1: transient failure. ---
            conn = ra._connect(db_path)
            outcome_1 = ra._verify_and_apply_decision(
                item=item, pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID,
                conn=conn, db_path=db_path,
            )
            conn.close()

            assert outcome_1 == "mint_failed"
            assert outcome_1 not in ra._ACK_OUTCOMES, "a transient failure must NOT be acked"

            row_after_tick1 = _row(db_path, proposal_id_str)
            assert row_after_tick1["status"] == "pending", "row must stay pending — nothing applied yet"
            assert row_after_tick1["token_hmac"] is None

            conn = ra._connect(db_path)
            nonce_row = conn.execute(
                "SELECT 1 FROM remote_approval_decision_nonces WHERE nonce = ?",
                (envelope["nonce"],),
            ).fetchone()
            conn.close()
            assert nonce_row is None, "the nonce must NOT be marked on a transient mint failure"

            assert await queue.claim_next() is None, "work item must NOT be re-enqueued on failure"

            # --- Tick 2: the cloud re-serves the SAME (never-acked) decision. ---
            conn = ra._connect(db_path)
            outcome_2 = ra._verify_and_apply_decision(
                item=item, pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID,
                conn=conn, db_path=db_path,
            )
            conn.close()

        assert outcome_2 == "applied", "the retry must succeed — NO permanent drop"
        assert outcome_2 in ra._ACK_OUTCOMES

        row_after_tick2 = _row(db_path, proposal_id_str)
        assert row_after_tick2["status"] == "approved"
        assert row_after_tick2["token_hmac"]

        # Work item re-enqueued this time.
        requeued_item = await queue.claim_next()
        assert requeued_item is not None and requeued_item.id == work_item_id

        # Re-dispatch with the minted token -> ACTUALLY EXECUTES.
        from uuid import UUID

        token = await gate.approved_token_for(UUID(proposal_id_str))
        proposal = _proposal(proposal_id=UUID(proposal_id_str))
        with patch(f"{_ENTERPRISE_ROUTE_MODULE}.tenant_remote_approval_enabled", return_value=True):
            final_outcome = await broker.dispatch(
                proposal, _ctx(), hitl_approval_token=token, work_item_id=work_item_id,
            )
        assert final_outcome.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1

    @pytest.mark.asyncio
    async def test_replay_of_already_applied_decision_acks_without_double_mint_or_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HERMES_AUDIT_KEY", _SIGNING_KEY_HEX)
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        broker, adapter = _make_broker(db_path=db_path, gate=gate)
        queue = SqliteWorkQueue(db_path=db_path)

        proposal_id_str, work_item_id = await _register_broker_row(
            db_path=db_path, broker=broker, queue=queue,
        )
        row_before = _row(db_path, proposal_id_str)

        private_key, pubkey_hex = _generate_keypair()
        request_id = str(uuid4())
        _seed_push_mapping(db_path, proposal_id=proposal_id_str, request_id=request_id)
        envelope = _build_envelope(
            proposal_id=proposal_id_str, action_digest=row_before["action_digest"] or "",
            request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)
        item = {**envelope, "signature_hex": signature_hex}

        # --- First application: succeeds normally. ---
        conn = ra._connect(db_path)
        first_outcome = ra._verify_and_apply_decision(
            item=item, pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID,
            conn=conn, db_path=db_path,
        )
        conn.close()
        assert first_outcome == "applied"

        from uuid import UUID

        token = await gate.approved_token_for(UUID(proposal_id_str))
        proposal = _proposal(proposal_id=UUID(proposal_id_str))
        with patch(f"{_ENTERPRISE_ROUTE_MODULE}.tenant_remote_approval_enabled", return_value=True):
            outcome = await broker.dispatch(
                proposal, _ctx(), hitl_approval_token=token, work_item_id=work_item_id,
            )
        assert outcome.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1

        # --- The cloud re-serves the SAME (already-applied) decision again. ---
        with (
            patch(
                "hermes.capabilities.infrastructure.sqlite_approval_gate.SqliteApprovalGate."
                "approve_enterprise_decision"
            ) as mock_mint,
            patch.object(SqliteWorkQueue, "re_enqueue_after_approval") as mock_reenqueue,
        ):
            conn = ra._connect(db_path)
            replay_outcome = ra._verify_and_apply_decision(
                item=item, pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID,
                conn=conn, db_path=db_path,
            )
            conn.close()
            mock_mint.assert_not_called()
            mock_reenqueue.assert_not_called()

        assert replay_outcome == "already_resolved"
        assert replay_outcome in ra._ACK_OUTCOMES

        # No double-execute: replaying the (single-use, already-consumed)
        # token again must NOT execute a second time.
        proposal_2 = _proposal(proposal_id=UUID(proposal_id_str))
        with patch(f"{_ENTERPRISE_ROUTE_MODULE}.tenant_remote_approval_enabled", return_value=True):
            second_dispatch = await broker.dispatch(
                proposal_2, _ctx(), hitl_approval_token=token, work_item_id=work_item_id,
            )
        assert second_dispatch.status != ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1, "adapter.replay must have run exactly once total"


class TestReenqueueConfirmationSelfHeals:
    """Adversarial-review MEDIUM finding (2026-07-06): the mint
    (`approve_enterprise_decision`) and the re-enqueue
    (`re_enqueue_after_approval`) are two SEPARATE, non-atomic autocommit
    transactions. A transient re-enqueue failure (or a crash between the
    two) previously either got silently swallowed into 'applied' (stranding
    the WorkItem in 'pending_approval' forever, ACKed so the cloud never
    re-serves) or, on restart, hit the early already_resolved guard and got
    ACKed without ever re-attempting the re-enqueue. This class proves both
    failure windows now self-heal.
    """

    @pytest.mark.asyncio
    async def test_transient_reenqueue_failure_then_retry_confirms_and_executes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HERMES_AUDIT_KEY", _SIGNING_KEY_HEX)
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        broker, adapter = _make_broker(db_path=db_path, gate=gate)
        queue = SqliteWorkQueue(db_path=db_path)

        proposal_id_str, work_item_id = await _register_broker_row(
            db_path=db_path, broker=broker, queue=queue,
        )
        row_before = _row(db_path, proposal_id_str)

        private_key, pubkey_hex = _generate_keypair()
        request_id = str(uuid4())
        _seed_push_mapping(db_path, proposal_id=proposal_id_str, request_id=request_id)
        envelope = _build_envelope(
            proposal_id=proposal_id_str, action_digest=row_before["action_digest"] or "",
            request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)
        item = {**envelope, "signature_hex": signature_hex}

        import sqlite3

        real_reenqueue = SqliteWorkQueue.re_enqueue_after_approval
        call_count = {"n": 0}

        async def _flaky_reenqueue(self, item_id):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return await real_reenqueue(self, item_id)

        with patch.object(SqliteWorkQueue, "re_enqueue_after_approval", _flaky_reenqueue):
            # --- Tick 1: mint commits durably, re-enqueue fails transiently. ---
            conn = ra._connect(db_path)
            outcome_1 = ra._verify_and_apply_decision(
                item=item, pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID,
                conn=conn, db_path=db_path,
            )
            conn.close()

            assert outcome_1 == "reenqueue_pending"
            assert outcome_1 not in ra._ACK_OUTCOMES, "a transient re-enqueue failure must NOT be acked"

            row_after_tick1 = _row(db_path, proposal_id_str)
            assert row_after_tick1["status"] == "approved", "the mint itself DID commit"
            assert row_after_tick1["token_hmac"]

            conn = ra._connect(db_path)
            nonce_row = conn.execute(
                "SELECT 1 FROM remote_approval_decision_nonces WHERE nonce = ?",
                (envelope["nonce"],),
            ).fetchone()
            conn.close()
            assert nonce_row is None, "the nonce must NOT be marked until re-enqueue is CONFIRMED"

            assert (
                ra._fetch_work_item_status(db_path, str(work_item_id)) == "pending_approval"
            ), "work item must still be stuck — re-enqueue never confirmed"

            # --- Tick 2: the cloud re-serves the SAME (never-acked) decision;
            # the row is now 'approved' (self-heal branch), retries the
            # re-enqueue, which succeeds this time. ---
            conn = ra._connect(db_path)
            outcome_2 = ra._verify_and_apply_decision(
                item=item, pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID,
                conn=conn, db_path=db_path,
            )
            conn.close()

        assert outcome_2 == "applied", "the retry must succeed — NO permanent drop"
        assert outcome_2 in ra._ACK_OUTCOMES
        assert call_count["n"] == 2, "re-enqueue must have been attempted exactly twice"

        assert ra._fetch_work_item_status(db_path, str(work_item_id)) == "pending"

        from uuid import UUID

        token = await gate.approved_token_for(UUID(proposal_id_str))
        proposal = _proposal(proposal_id=UUID(proposal_id_str))
        with patch(f"{_ENTERPRISE_ROUTE_MODULE}.tenant_remote_approval_enabled", return_value=True):
            final_outcome = await broker.dispatch(
                proposal, _ctx(), hitl_approval_token=token, work_item_id=work_item_id,
            )
        assert final_outcome.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1

    @pytest.mark.asyncio
    async def test_crash_after_mint_before_reenqueue_self_heals_and_executes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulates a process kill AFTER the mint committed but BEFORE the
        re-enqueue ran at all (not even a failed attempt — none was made).
        The row is already 'approved'+token-minted; the work item is still
        'pending_approval'. The NEXT re-serve of the SAME verified decision
        must self-heal (re-enqueue + confirm + execute), not get stuck
        behind the already_resolved guard forever."""
        monkeypatch.setenv("HERMES_AUDIT_KEY", _SIGNING_KEY_HEX)
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        broker, adapter = _make_broker(db_path=db_path, gate=gate)
        queue = SqliteWorkQueue(db_path=db_path)

        proposal_id_str, work_item_id = await _register_broker_row(
            db_path=db_path, broker=broker, queue=queue,
        )
        row_before = _row(db_path, proposal_id_str)

        # Simulate "mint succeeded on a prior tick, then the process died
        # before ever attempting the re-enqueue" — call the mint directly,
        # bypassing _mint_and_reenqueue_broker_row's re-enqueue step entirely.
        from uuid import UUID

        await gate.approve_enterprise_decision(proposal_id=UUID(proposal_id_str))
        row_mid_crash = _row(db_path, proposal_id_str)
        assert row_mid_crash["status"] == "approved"
        assert row_mid_crash["token_hmac"]
        assert ra._fetch_work_item_status(db_path, str(work_item_id)) == "pending_approval"

        private_key, pubkey_hex = _generate_keypair()
        request_id = str(uuid4())
        _seed_push_mapping(db_path, proposal_id=proposal_id_str, request_id=request_id)
        envelope = _build_envelope(
            proposal_id=proposal_id_str, action_digest=row_before["action_digest"] or "",
            request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)

        conn = ra._connect(db_path)
        outcome = ra._verify_and_apply_decision(
            item={**envelope, "signature_hex": signature_hex},
            pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID,
            conn=conn, db_path=db_path,
        )
        conn.close()

        assert outcome == "applied"
        assert outcome in ra._ACK_OUTCOMES
        assert ra._fetch_work_item_status(db_path, str(work_item_id)) == "pending"

        token = await gate.approved_token_for(UUID(proposal_id_str))
        proposal = _proposal(proposal_id=UUID(proposal_id_str))
        with patch(f"{_ENTERPRISE_ROUTE_MODULE}.tenant_remote_approval_enabled", return_value=True):
            final_outcome = await broker.dispatch(
                proposal, _ctx(), hitl_approval_token=token, work_item_id=work_item_id,
            )
        assert final_outcome.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1
