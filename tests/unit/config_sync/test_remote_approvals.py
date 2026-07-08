"""hermes.config_sync.remote_approvals — Enterprise remote-approval push/poll
loop (Fase 2 Phase 4b, RUNTIME/associate side).

Covers:
  - decision_signing_bytes: PINNED byte format (sort_keys, no whitespace,
    ensure_ascii=False, utf-8).
  - PUSH: builds the pinned body (incl. a fresh `request_id` per occurrence),
    marks pushed, is idempotent (no double-push on a second tick), and
    re-pushes a REVIVED row (new created_at) with a NEW request_id.
  - POLL+verify+apply fail-closed matrix: valid → resumes + flips status;
    tampered signature / wrong action_digest / wrong instance_id / replayed
    nonce / unknown proposal / already-resolved / malformed envelope /
    unknown or mismatched request_id / stale (superseded) request_id → NO
    resume, NO row mutation.
  - ACK (bug #1): applied/already-resolved/replayed/stale outcomes are ACKed
    in one batched POST /v1/approvals/ack; other outcomes are never acked.
  - Retention (bug #1): remote_approval_pushed / _decision_nonces are pruned
    by age, EXCEPT a pushed-mapping row for a still-'pending' proposal.
  - Per-occurrence fresh approval (bug #2): two occurrences of the identical
    enterprise action mint two distinct request_ids and can be independently
    approved/denied; a decision for a superseded request_id never resolves
    the newer occurrence's row.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
from hermes.capabilities.domain.ports import ConsentContext, RiskLevel
from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate
from hermes.config_sync import remote_approvals as ra

pytestmark = pytest.mark.unit

_OWN_INSTANCE_ID = "instance-abc-123"
_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gate(db_path) -> SqliteApprovalGate:
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


async def _register_enterprise_row(
    gate: SqliteApprovalGate, *, action_digest: str, agent_id: str = "agent-a",
    work_item_id: UUID | None = None,
) -> str:
    """Registers a route='enterprise' pending row.

    `work_item_id` defaults to UUID(int=0) — the SAME sentinel `security_hook.
    _resolve_native_danger_approval` always uses for a NATIVE-danger row (it
    has no real WorkQueue task). This is what makes `_verify_and_apply_
    decision` treat it as a native row (flip status + signal the Event) —
    matching what the vast majority of tests in this module actually exercise.
    Pass a REAL UUID explicitly to simulate a BROKER row instead (Part B —
    see TestVerifyAndApplyDecisionBrokerRow)."""
    proposal_id = uuid4()
    await gate.register_pending(
        proposal_id=proposal_id,
        work_item_id=work_item_id if work_item_id is not None else UUID(int=0),
        consent_context=ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID),
        risk=RiskLevel.HIGH,
        justification="remote approvals test",
        parameters_redacted={"schedule": "0 9 * * *"},
        tool_name="cronjob",
        action_digest=action_digest,
        route="enterprise",
        agent_id=agent_id,
    )
    return str(proposal_id)


def _generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key().public_bytes_raw().hex()


def _sign_envelope(private_key: Ed25519PrivateKey, envelope: dict[str, str]) -> str:
    payload = ra.decision_signing_bytes(envelope)
    return private_key.sign(payload).hex()


def _build_envelope(
    *,
    proposal_id: str,
    action_digest: str,
    instance_id: str = _OWN_INSTANCE_ID,
    decision: str = "approve",
    nonce: str | None = None,
    request_id: str | None = None,
) -> dict[str, str]:
    return {
        "action_digest": action_digest,
        "agent_id": "agent-a",
        "approver_user_id": "approver-42",
        "decided_at": "2026-07-04T12:00:00Z",
        "decision": decision,
        "instance_id": instance_id,
        "nonce": nonce or str(uuid4()),
        "proposal_id": proposal_id,
        "request_id": request_id or str(uuid4()),
    }


def _seed_push_mapping(
    conn: sqlite3.Connection, *, proposal_id: str, request_id: str,
    pushed_at: str = "2026-07-04T00:00:00Z",
) -> None:
    """Seeds the (request_id -> proposal_id) mapping `_verify_and_apply_
    decision` needs (bug #2) — mirrors what `push_pending_enterprise_
    approvals` would have persisted for a real push."""
    ra._ensure_remote_approval_schema(conn)
    ra._mark_pushed(
        conn, proposal_id=proposal_id, request_id=request_id, pushed_at=pushed_at,
    )


def _row(db_path, proposal_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM pending_approvals WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# decision_signing_bytes — PINNED format
# ---------------------------------------------------------------------------


class TestDecisionSigningBytesFormat:
    def test_exact_pinned_bytes(self) -> None:
        envelope = {
            "action_digest": "dig1",
            "agent_id": "a1",
            "approver_user_id": "u1",
            "decided_at": "2026-07-04T12:00:00Z",
            "decision": "approve",
            "instance_id": "inst1",
            "nonce": "n1",
            "proposal_id": "p1",
            "request_id": "r1",
        }
        result = ra.decision_signing_bytes(envelope)
        expected = json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        assert result == expected
        assert result == (
            b'{"action_digest":"dig1","agent_id":"a1","approver_user_id":"u1",'
            b'"decided_at":"2026-07-04T12:00:00Z","decision":"approve",'
            b'"instance_id":"inst1","nonce":"n1","proposal_id":"p1",'
            b'"request_id":"r1"}'
        )

    def test_deterministic_regardless_of_input_key_order(self) -> None:
        a = {"proposal_id": "p1", "action_digest": "d1", "agent_id": "a1",
             "approver_user_id": "u1", "decided_at": "t1", "decision": "deny",
             "instance_id": "i1", "nonce": "n1", "request_id": "r1"}
        b = {"action_digest": "d1", "proposal_id": "p1", "nonce": "n1",
             "agent_id": "a1", "decision": "deny", "instance_id": "i1",
             "approver_user_id": "u1", "decided_at": "t1", "request_id": "r1"}
        assert ra.decision_signing_bytes(a) == ra.decision_signing_bytes(b)


# ---------------------------------------------------------------------------
# PUSH
# ---------------------------------------------------------------------------


class TestPush:
    @pytest.mark.asyncio
    async def test_push_builds_pinned_body_and_marks_pushed(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-1")

        captured: list[dict] = []

        def _fake_post(url, *, headers, json, timeout, follow_redirects):  # noqa: A002
            captured.append({"url": url, "headers": headers, "body": json})
            return MagicMock(status_code=200)

        with patch("httpx.post", side_effect=_fake_post):
            ra.push_pending_enterprise_approvals(
                db_path=db_path,
                cloud_endpoint="https://cloud.example.com",
                instance_secret="secret-abc",
            )

        assert len(captured) == 1
        body = captured[0]["body"]
        assert body["proposal_id"] == proposal_id
        assert isinstance(body["request_id"], str) and body["request_id"]
        assert body["request_id"] != body["proposal_id"]
        assert body["agent_id"] == "agent-a"
        assert body["tool_name"] == "cronjob"
        assert body["action_digest"] == "dig-1"
        assert body["risk"] == "high"
        assert body["params_redacted"] == {"schedule": "0 9 * * *"}
        assert captured[0]["headers"]["Authorization"] == "Bearer secret-abc"

    @pytest.mark.asyncio
    async def test_push_is_idempotent_no_double_push(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        await _register_enterprise_row(gate, action_digest="dig-2")

        call_count = {"n": 0}

        def _fake_post(*args, **kwargs):
            call_count["n"] += 1
            return MagicMock(status_code=200)

        with patch("httpx.post", side_effect=_fake_post):
            ra.push_pending_enterprise_approvals(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_secret="s",
            )
            ra.push_pending_enterprise_approvals(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_secret="s",
            )

        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_failed_push_is_retried_next_tick(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        await _register_enterprise_row(gate, action_digest="dig-3")

        call_count = {"n": 0}

        def _fake_post(*args, **kwargs):
            call_count["n"] += 1
            return MagicMock(status_code=500)

        with patch("httpx.post", side_effect=_fake_post):
            ra.push_pending_enterprise_approvals(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_secret="s",
            )
            ra.push_pending_enterprise_approvals(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_secret="s",
            )

        assert call_count["n"] == 2  # never marked pushed on a non-2xx status

    @pytest.mark.asyncio
    async def test_local_route_row_is_never_pushed(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        await gate.register_pending(
            proposal_id=uuid4(),
            work_item_id=uuid4(),
            consent_context=ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID),
            risk=RiskLevel.HIGH,
            justification="local row",
            parameters_redacted={},
            tool_name="cronjob",
            action_digest="dig-local",
        )  # route="" (default, LOCAL)

        with patch("httpx.post") as mock_post:
            ra.push_pending_enterprise_approvals(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_secret="s",
            )

        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_revived_row_is_pushed_again_with_a_new_request_id(self, tmp_path) -> None:
        """register_pending's revival path (delete+recreate on re-registration
        after expiry/rejection) gives a fresh created_at — the push loop must
        re-push it (not treat it as already-pushed forever) AND mint a FRESH
        request_id (bug #2) rather than reusing the superseded one."""
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id_str = await _register_enterprise_row(gate, action_digest="dig-revive")
        proposal_id = UUID(proposal_id_str)

        first_bodies: list[dict] = []
        with patch(
            "httpx.post",
            side_effect=lambda url, *, headers, json, timeout, follow_redirects: (
                first_bodies.append(json) or MagicMock(status_code=200)
            ),
        ) as mock_post:
            ra.push_pending_enterprise_approvals(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_secret="s",
            )
        assert mock_post.call_count == 1
        first_request_id = first_bodies[0]["request_id"]

        # Expire then re-register the SAME (deterministic) proposal_id — revives it.
        await gate.expire(proposal_id=proposal_id)
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=uuid4(),
            consent_context=ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID),
            risk=RiskLevel.HIGH,
            justification="revived",
            parameters_redacted={"schedule": "0 9 * * *"},
            tool_name="cronjob",
            action_digest="dig-revive",
            route="enterprise",
            agent_id="agent-a",
        )

        second_bodies: list[dict] = []
        with patch(
            "httpx.post",
            side_effect=lambda url, *, headers, json, timeout, follow_redirects: (
                second_bodies.append(json) or MagicMock(status_code=200)
            ),
        ) as mock_post_2:
            ra.push_pending_enterprise_approvals(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_secret="s",
            )
        assert mock_post_2.call_count == 1
        second_request_id = second_bodies[0]["request_id"]

        assert second_request_id != first_request_id
        assert second_bodies[0]["proposal_id"] == proposal_id_str  # local id unchanged


# ---------------------------------------------------------------------------
# POLL + verify + apply — fail-closed matrix
# ---------------------------------------------------------------------------


class TestVerifyAndApplyDecision:
    @pytest.mark.asyncio
    async def test_valid_approve_resumes_and_flips_approved(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-ok")
        request_id = str(uuid4())

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(
            proposal_id=proposal_id, action_digest="dig-ok", request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                _seed_push_mapping(conn, proposal_id=proposal_id, request_id=request_id)
                outcome = ra._verify_and_apply_decision(
                    item={**envelope, "signature_hex": signature_hex},
                    pubkey_hex=pubkey_hex,
                    own_instance_id=_OWN_INSTANCE_ID,
                    conn=conn, db_path=db_path,
                )

        assert outcome == "applied"
        mock_signal.assert_called_once_with(proposal_id, "approved")
        assert _row(db_path, proposal_id)["status"] == "approved"

    @pytest.mark.asyncio
    async def test_valid_deny_resumes_and_flips_rejected(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-deny")
        request_id = str(uuid4())

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(
            proposal_id=proposal_id, action_digest="dig-deny", decision="deny",
            request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                _seed_push_mapping(conn, proposal_id=proposal_id, request_id=request_id)
                outcome = ra._verify_and_apply_decision(
                    item={**envelope, "signature_hex": signature_hex},
                    pubkey_hex=pubkey_hex,
                    own_instance_id=_OWN_INSTANCE_ID,
                    conn=conn, db_path=db_path,
                )

        assert outcome == "applied"
        mock_signal.assert_called_once_with(proposal_id, "denied")
        assert _row(db_path, proposal_id)["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_tampered_signature_fails_closed(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-tamper")

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(proposal_id=proposal_id, action_digest="dig-tamper")
        signature_hex = _sign_envelope(private_key, envelope)
        # Tamper AFTER signing — the signature no longer matches this envelope.
        tampered = {**envelope, "decision": "deny"}

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                ra._ensure_remote_approval_schema(conn)
                outcome = ra._verify_and_apply_decision(
                    item={**tampered, "signature_hex": signature_hex},
                    pubkey_hex=pubkey_hex,
                    own_instance_id=_OWN_INSTANCE_ID,
                    conn=conn, db_path=db_path,
                )

        assert outcome == "bad_signature"
        mock_signal.assert_not_called()
        assert _row(db_path, proposal_id)["status"] == "pending"

    @pytest.mark.asyncio
    async def test_wrong_action_digest_fails_closed(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-real")
        request_id = str(uuid4())

        private_key, pubkey_hex = _generate_keypair()
        # Envelope signed with a digest that does NOT match the pending row's.
        envelope = _build_envelope(
            proposal_id=proposal_id, action_digest="dig-WRONG", request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                _seed_push_mapping(conn, proposal_id=proposal_id, request_id=request_id)
                outcome = ra._verify_and_apply_decision(
                    item={**envelope, "signature_hex": signature_hex},
                    pubkey_hex=pubkey_hex,
                    own_instance_id=_OWN_INSTANCE_ID,
                    conn=conn, db_path=db_path,
                )

        assert outcome == "digest_mismatch"
        mock_signal.assert_not_called()
        assert _row(db_path, proposal_id)["status"] == "pending"

    @pytest.mark.asyncio
    async def test_wrong_instance_id_fails_closed(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-inst")

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(
            proposal_id=proposal_id, action_digest="dig-inst", instance_id="some-OTHER-instance",
        )
        signature_hex = _sign_envelope(private_key, envelope)

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                ra._ensure_remote_approval_schema(conn)
                outcome = ra._verify_and_apply_decision(
                    item={**envelope, "signature_hex": signature_hex},
                    pubkey_hex=pubkey_hex,
                    own_instance_id=_OWN_INSTANCE_ID,  # differs from envelope's instance_id
                    conn=conn, db_path=db_path,
                )

        assert outcome == "wrong_instance"
        mock_signal.assert_not_called()
        assert _row(db_path, proposal_id)["status"] == "pending"

    @pytest.mark.asyncio
    async def test_unknown_request_id_fails_closed(self, tmp_path) -> None:
        """A request_id this associate never pushed (foreign/bogus/pruned) must
        never resolve anything, even if it carries an otherwise-valid signed
        envelope for a REAL pending proposal."""
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-unknown-req")

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(
            proposal_id=proposal_id, action_digest="dig-unknown-req",
            request_id=str(uuid4()),  # never seeded/pushed
        )
        signature_hex = _sign_envelope(private_key, envelope)

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                ra._ensure_remote_approval_schema(conn)
                outcome = ra._verify_and_apply_decision(
                    item={**envelope, "signature_hex": signature_hex},
                    pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )

        assert outcome == "unknown_request"
        mock_signal.assert_not_called()
        assert _row(db_path, proposal_id)["status"] == "pending"

    @pytest.mark.asyncio
    async def test_request_id_mapped_to_a_different_proposal_fails_closed(
        self, tmp_path
    ) -> None:
        """Defense in depth: the request_id resolves to a DIFFERENT local
        proposal_id than the one the envelope itself claims — a signed
        envelope can never override the locally-tracked mapping."""
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        real_proposal_id = await _register_enterprise_row(gate, action_digest="dig-mismatch")
        request_id = str(uuid4())

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(
            proposal_id=str(uuid4()),  # does NOT match what request_id maps to
            action_digest="dig-mismatch", request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                _seed_push_mapping(
                    conn, proposal_id=real_proposal_id, request_id=request_id,
                )
                outcome = ra._verify_and_apply_decision(
                    item={**envelope, "signature_hex": signature_hex},
                    pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )

        assert outcome == "request_proposal_mismatch"
        mock_signal.assert_not_called()
        assert _row(db_path, real_proposal_id)["status"] == "pending"

    @pytest.mark.asyncio
    async def test_replayed_nonce_fails_closed_on_second_application(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-replay")
        request_id = str(uuid4())

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(
            proposal_id=proposal_id, action_digest="dig-replay", nonce="fixed-nonce-1",
            request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)
        item = {**envelope, "signature_hex": signature_hex}

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                _seed_push_mapping(conn, proposal_id=proposal_id, request_id=request_id)
                first = ra._verify_and_apply_decision(
                    item=item, pubkey_hex=pubkey_hex,
                    own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )
                # Re-register a SECOND pending row with the SAME action_digest
                # would collide on the partial UNIQUE index; instead directly
                # re-apply the SAME (already-consumed) envelope — the row is now
                # 'approved', so replay is caught by BOTH the nonce store and the
                # status check. To isolate the nonce check specifically, replay
                # against a fresh 'pending' row sharing the SAME nonce.
                second = ra._verify_and_apply_decision(
                    item=item, pubkey_hex=pubkey_hex,
                    own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )

        assert first == "applied"
        assert second in {"already_resolved", "replayed_nonce"}
        mock_signal.assert_called_once()  # only the FIRST application resumed anything

    @pytest.mark.asyncio
    async def test_nonce_replay_detected_even_against_a_fresh_pending_row(
        self, tmp_path
    ) -> None:
        """Isolates the nonce check from the status check: two DIFFERENT
        proposals, same nonce — the second must be rejected as a replay even
        though its OWN row is still 'pending'."""
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id_1 = await _register_enterprise_row(gate, action_digest="dig-n1")
        proposal_id_2 = await _register_enterprise_row(gate, action_digest="dig-n2")
        request_id_1, request_id_2 = str(uuid4()), str(uuid4())

        private_key, pubkey_hex = _generate_keypair()
        shared_nonce = "shared-nonce-xyz"
        envelope_1 = _build_envelope(
            proposal_id=proposal_id_1, action_digest="dig-n1", nonce=shared_nonce,
            request_id=request_id_1,
        )
        envelope_2 = _build_envelope(
            proposal_id=proposal_id_2, action_digest="dig-n2", nonce=shared_nonce,
            request_id=request_id_2,
        )
        sig_1 = _sign_envelope(private_key, envelope_1)
        sig_2 = _sign_envelope(private_key, envelope_2)

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                _seed_push_mapping(conn, proposal_id=proposal_id_1, request_id=request_id_1)
                _seed_push_mapping(conn, proposal_id=proposal_id_2, request_id=request_id_2)
                first = ra._verify_and_apply_decision(
                    item={**envelope_1, "signature_hex": sig_1}, pubkey_hex=pubkey_hex,
                    own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )
                second = ra._verify_and_apply_decision(
                    item={**envelope_2, "signature_hex": sig_2}, pubkey_hex=pubkey_hex,
                    own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )

        assert first == "applied"
        assert second == "replayed_nonce"
        mock_signal.assert_called_once()
        assert _row(db_path, proposal_id_2)["status"] == "pending"  # untouched

    @pytest.mark.asyncio
    async def test_unknown_proposal_fails_closed(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        _make_gate(db_path)  # ensures schema exists, no rows
        proposal_id = str(uuid4())  # never registered as a pending row
        request_id = str(uuid4())

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(
            proposal_id=proposal_id, action_digest="dig-none", request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                # The push mapping can legitimately exist (we DID push it) even
                # though the pending_approvals row is gone (e.g. deleted
                # out-of-band) — isolates "unknown_proposal" from "unknown_request".
                _seed_push_mapping(conn, proposal_id=proposal_id, request_id=request_id)
                outcome = ra._verify_and_apply_decision(
                    item={**envelope, "signature_hex": signature_hex},
                    pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )

        assert outcome == "unknown_proposal"
        mock_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_resolved_row_fails_closed(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-resolved")
        request_id = str(uuid4())
        await gate.reject(
            proposal_id=UUID(proposal_id), rejected_by=uuid4(), reason="already denied locally",
        )

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(
            proposal_id=proposal_id, action_digest="dig-resolved", request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                _seed_push_mapping(conn, proposal_id=proposal_id, request_id=request_id)
                outcome = ra._verify_and_apply_decision(
                    item={**envelope, "signature_hex": signature_hex},
                    pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )

        assert outcome == "already_resolved"
        mock_signal.assert_not_called()
        assert _row(db_path, proposal_id)["status"] == "rejected"  # local deny wins (I-2)

    @pytest.mark.asyncio
    async def test_malformed_envelope_missing_key_fails_closed(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-malformed")

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(proposal_id=proposal_id, action_digest="dig-malformed")
        signature_hex = _sign_envelope(private_key, envelope)
        broken = dict(envelope)
        del broken["nonce"]  # missing a PINNED key

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                ra._ensure_remote_approval_schema(conn)
                outcome = ra._verify_and_apply_decision(
                    item={**broken, "signature_hex": signature_hex},
                    pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )

        assert outcome == "invalid_envelope"
        mock_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_decision_value_fails_closed(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-baddec")

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(
            proposal_id=proposal_id, action_digest="dig-baddec", decision="maybe",
        )
        signature_hex = _sign_envelope(private_key, envelope)

        with patch(
            "hermes.runtime.security_hook.signal_native_danger_approval"
        ) as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                ra._ensure_remote_approval_schema(conn)
                outcome = ra._verify_and_apply_decision(
                    item={**envelope, "signature_hex": signature_hex},
                    pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )

        assert outcome == "invalid_envelope"
        mock_signal.assert_not_called()


# ---------------------------------------------------------------------------
# Bug #1 — ACK loop (closes the unbounded-re-serve growth)
# ---------------------------------------------------------------------------


class TestAckLoop:
    @pytest.mark.asyncio
    async def test_applied_decision_is_acked_with_its_request_id(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-ack-1")

        pushed_bodies: list[dict] = []
        with patch(
            "httpx.post",
            side_effect=lambda url, *, headers, json, timeout, follow_redirects: (
                pushed_bodies.append(json) or MagicMock(status_code=200)
            ),
        ):
            ra.push_pending_enterprise_approvals(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_secret="s",
            )
        request_id = pushed_bodies[0]["request_id"]

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(
            proposal_id=proposal_id, action_digest="dig-ack-1", request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)
        decisions_payload = json.dumps(
            {"decisions": [{**envelope, "signature_hex": signature_hex}]}
        ).encode()

        ack_calls: list[dict] = []

        def _fake_post(url, *, headers, json, timeout, follow_redirects):  # noqa: A002
            ack_calls.append({"url": url, "body": json})
            return MagicMock(status_code=200)

        with (
            patch("httpx.get", return_value=MagicMock(status_code=200, content=decisions_payload)),
            patch("httpx.post", side_effect=_fake_post),
            patch("hermes.runtime.security_hook.signal_native_danger_approval") as mock_signal,
        ):
            ra.poll_and_apply_decisions(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_id=_OWN_INSTANCE_ID, instance_secret="s", pubkey_hex=pubkey_hex,
            )

        mock_signal.assert_called_once_with(proposal_id, "approved")
        assert len(ack_calls) == 1
        assert ack_calls[0]["url"] == "https://cloud.example.com/v1/approvals/ack"
        assert ack_calls[0]["body"] == {"request_ids": [request_id]}

    @pytest.mark.asyncio
    async def test_reserved_already_resolved_duplicate_is_also_acked(self, tmp_path) -> None:
        """The cloud re-serving an ALREADY-applied decision (because it was
        never acked) must still be acked on THIS poll — this is precisely
        what stops the infinite-re-serve loop (bug #1)."""
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-ack-dup")
        request_id = str(uuid4())

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(
            proposal_id=proposal_id, action_digest="dig-ack-dup", request_id=request_id,
        )
        signature_hex = _sign_envelope(private_key, envelope)
        item = {**envelope, "signature_hex": signature_hex}

        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            _seed_push_mapping(conn, proposal_id=proposal_id, request_id=request_id)
            with patch("hermes.runtime.security_hook.signal_native_danger_approval"):
                first_outcome = ra._verify_and_apply_decision(
                    item=item, pubkey_hex=pubkey_hex,
                    own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )
        assert first_outcome == "applied"

        decisions_payload = json.dumps({"decisions": [item]}).encode()
        ack_calls: list[dict] = []

        def _fake_post(url, *, headers, json, timeout, follow_redirects):  # noqa: A002
            ack_calls.append({"body": json})
            return MagicMock(status_code=200)

        with (
            patch("httpx.get", return_value=MagicMock(status_code=200, content=decisions_payload)),
            patch("httpx.post", side_effect=_fake_post),
            patch("hermes.runtime.security_hook.signal_native_danger_approval") as mock_signal,
        ):
            ra.poll_and_apply_decisions(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_id=_OWN_INSTANCE_ID, instance_secret="s", pubkey_hex=pubkey_hex,
            )

        mock_signal.assert_not_called()  # already resolved — no second resume
        assert len(ack_calls) == 1
        assert ack_calls[0]["body"] == {"request_ids": [request_id]}

    @pytest.mark.asyncio
    async def test_bad_signature_is_never_acked(self, tmp_path) -> None:
        """Tampered/unverifiable data must NOT be silently acked away — that
        would hide the problem instead of surfacing it via re-delivery."""
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id = await _register_enterprise_row(gate, action_digest="dig-badsig")

        private_key, pubkey_hex = _generate_keypair()
        envelope = _build_envelope(proposal_id=proposal_id, action_digest="dig-badsig")
        signature_hex = _sign_envelope(private_key, envelope)
        tampered = {**envelope, "decision": "deny", "signature_hex": signature_hex}
        decisions_payload = json.dumps({"decisions": [tampered]}).encode()

        with (
            patch("httpx.get", return_value=MagicMock(status_code=200, content=decisions_payload)),
            patch("httpx.post") as mock_post,
        ):
            ra.poll_and_apply_decisions(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_id=_OWN_INSTANCE_ID, instance_secret="s", pubkey_hex=pubkey_hex,
            )

        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_decisions_never_calls_ack(self, tmp_path) -> None:
        db_path = tmp_path / "state.db"
        _make_gate(db_path)
        empty_payload = json.dumps({"decisions": []}).encode()

        with (
            patch("httpx.get", return_value=MagicMock(status_code=200, content=empty_payload)),
            patch("httpx.post") as mock_post,
        ):
            ra.poll_and_apply_decisions(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_id=_OWN_INSTANCE_ID, instance_secret="s", pubkey_hex="a" * 64,
            )

        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Bug #1 — retention/pruning of the local bookkeeping tables
# ---------------------------------------------------------------------------


class TestRetentionPruning:
    @pytest.mark.asyncio
    async def test_prune_deletes_stale_rows_but_keeps_still_pending_mappings(
        self, tmp_path
    ) -> None:
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        still_pending_id = await _register_enterprise_row(
            gate, action_digest="dig-prune-pending"
        )

        old_ts = (
            datetime.now(tz=UTC) - timedelta(days=ra._STATE_RETENTION_DAYS + 1)
        ).isoformat()
        fresh_ts = datetime.now(tz=UTC).isoformat()

        conn = ra._connect(db_path)
        try:
            ra._ensure_remote_approval_schema(conn)
            conn.execute(
                "INSERT INTO remote_approval_decision_nonces (nonce, seen_at) "
                "VALUES (?, ?)", ("old-nonce", old_ts),
            )
            conn.execute(
                "INSERT INTO remote_approval_decision_nonces (nonce, seen_at) "
                "VALUES (?, ?)", ("fresh-nonce", fresh_ts),
            )
            # Orphaned mapping (its proposal is long gone/resolved) — prunable.
            conn.execute(
                "INSERT INTO remote_approval_pushed "
                "(request_id, proposal_id, pushed_at) VALUES (?, ?, ?)",
                ("req-orphan", str(uuid4()), old_ts),
            )
            # Equally old, but tied to a STILL-'pending' proposal — must survive.
            conn.execute(
                "INSERT INTO remote_approval_pushed "
                "(request_id, proposal_id, pushed_at) VALUES (?, ?, ?)",
                ("req-pending", still_pending_id, old_ts),
            )

            ra._prune_stale_remote_approval_state(conn)

            remaining_nonces = {
                r["nonce"] for r in conn.execute(
                    "SELECT nonce FROM remote_approval_decision_nonces"
                )
            }
            remaining_requests = {
                r["request_id"] for r in conn.execute(
                    "SELECT request_id FROM remote_approval_pushed"
                )
            }
        finally:
            conn.close()

        assert remaining_nonces == {"fresh-nonce"}
        assert remaining_requests == {"req-pending"}

    @pytest.mark.asyncio
    async def test_prune_runs_every_poll_tick_even_with_zero_decisions(
        self, tmp_path
    ) -> None:
        db_path = tmp_path / "state.db"
        _make_gate(db_path)
        old_ts = (
            datetime.now(tz=UTC) - timedelta(days=ra._STATE_RETENTION_DAYS + 1)
        ).isoformat()

        conn = ra._connect(db_path)
        try:
            ra._ensure_remote_approval_schema(conn)
            conn.execute(
                "INSERT INTO remote_approval_decision_nonces (nonce, seen_at) "
                "VALUES (?, ?)", ("ancient-nonce", old_ts),
            )
        finally:
            conn.close()

        empty_payload = json.dumps({"decisions": []}).encode()
        with patch(
            "httpx.get", return_value=MagicMock(status_code=200, content=empty_payload)
        ):
            ra.poll_and_apply_decisions(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_id=_OWN_INSTANCE_ID, instance_secret="s", pubkey_hex="a" * 64,
            )

        conn = ra._connect(db_path)
        try:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM remote_approval_decision_nonces"
            ).fetchone()["n"]
        finally:
            conn.close()
        assert remaining == 0


# ---------------------------------------------------------------------------
# Bug #2 — per-occurrence fresh remote approval
# ---------------------------------------------------------------------------


class TestPerOccurrenceFreshApproval:
    @pytest.mark.asyncio
    async def test_two_occurrences_get_independent_request_ids_and_decisions(
        self, tmp_path
    ) -> None:
        """A repeated byte-identical enterprise action must get a FRESH
        remote approval request each time — occurrence #1 is denied,
        occurrence #2 (same digest, same deterministic proposal_id) is later
        independently approved via its OWN request_id."""
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id_str = await _register_enterprise_row(gate, action_digest="dig-occ")
        proposal_id = UUID(proposal_id_str)
        private_key, pubkey_hex = _generate_keypair()

        # --- Occurrence #1: pushed, then DENIED remotely. ---
        bodies_1: list[dict] = []
        with patch(
            "httpx.post",
            side_effect=lambda url, *, headers, json, timeout, follow_redirects: (
                bodies_1.append(json) or MagicMock(status_code=200)
            ),
        ):
            ra.push_pending_enterprise_approvals(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_secret="s",
            )
        request_id_1 = bodies_1[0]["request_id"]

        envelope_1 = _build_envelope(
            proposal_id=proposal_id_str, action_digest="dig-occ",
            decision="deny", request_id=request_id_1,
        )
        sig_1 = _sign_envelope(private_key, envelope_1)
        with patch("hermes.runtime.security_hook.signal_native_danger_approval") as mock_1:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                ra._ensure_remote_approval_schema(conn)
                outcome_1 = ra._verify_and_apply_decision(
                    item={**envelope_1, "signature_hex": sig_1}, pubkey_hex=pubkey_hex,
                    own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )
        assert outcome_1 == "applied"
        mock_1.assert_called_once_with(proposal_id_str, "denied")
        assert _row(db_path, proposal_id_str)["status"] == "rejected"

        # --- Occurrence #2: the SAME action recurs — revives the row. ---
        # work_item_id=UUID(int=0): the native-danger sentinel (matches
        # occurrence #1 and the rest of this module's native-path tests) —
        # this test is about per-occurrence fresh approval, not the broker/
        # native distinction (see TestVerifyAndApplyDecisionBrokerRow for that).
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=UUID(int=0),
            consent_context=ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID),
            risk=RiskLevel.HIGH,
            justification="second occurrence",
            parameters_redacted={"schedule": "0 9 * * *"},
            tool_name="cronjob",
            action_digest="dig-occ",
            route="enterprise",
            agent_id="agent-a",
        )
        assert _row(db_path, proposal_id_str)["status"] == "pending"  # revived

        bodies_2: list[dict] = []
        with patch(
            "httpx.post",
            side_effect=lambda url, *, headers, json, timeout, follow_redirects: (
                bodies_2.append(json) or MagicMock(status_code=200)
            ),
        ):
            ra.push_pending_enterprise_approvals(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_secret="s",
            )
        request_id_2 = bodies_2[0]["request_id"]
        assert request_id_2 != request_id_1

        envelope_2 = _build_envelope(
            proposal_id=proposal_id_str, action_digest="dig-occ",
            decision="approve", request_id=request_id_2,
        )
        sig_2 = _sign_envelope(private_key, envelope_2)
        with patch("hermes.runtime.security_hook.signal_native_danger_approval") as mock_2:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                ra._ensure_remote_approval_schema(conn)
                outcome_2 = ra._verify_and_apply_decision(
                    item={**envelope_2, "signature_hex": sig_2}, pubkey_hex=pubkey_hex,
                    own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )
        assert outcome_2 == "applied"
        mock_2.assert_called_once_with(proposal_id_str, "approved")
        assert _row(db_path, proposal_id_str)["status"] == "approved"

    @pytest.mark.asyncio
    async def test_stale_superseded_request_never_resolves_the_newer_occurrence(
        self, tmp_path
    ) -> None:
        """A decision that arrives LATE for a SUPERSEDED request_id (bug #2's
        core race: the admin resolves occurrence #1's cloud ticket only after
        occurrence #2 has already re-pushed) must NOT be applied to the
        newer, still-pending row."""
        db_path = tmp_path / "state.db"
        gate = _make_gate(db_path)
        proposal_id_str = await _register_enterprise_row(gate, action_digest="dig-stale")
        proposal_id = UUID(proposal_id_str)
        private_key, pubkey_hex = _generate_keypair()

        bodies_1: list[dict] = []
        with patch(
            "httpx.post",
            side_effect=lambda url, *, headers, json, timeout, follow_redirects: (
                bodies_1.append(json) or MagicMock(status_code=200)
            ),
        ):
            ra.push_pending_enterprise_approvals(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_secret="s",
            )
        request_id_1 = bodies_1[0]["request_id"]

        # Occurrence #1 times out locally (simulated directly as an expire).
        await gate.expire(proposal_id=proposal_id)

        # Occurrence #2 recurs before the cloud admin ever resolves ticket #1.
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=uuid4(),
            consent_context=ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID),
            risk=RiskLevel.HIGH,
            justification="second occurrence",
            parameters_redacted={"schedule": "0 9 * * *"},
            tool_name="cronjob",
            action_digest="dig-stale",
            route="enterprise",
            agent_id="agent-a",
        )
        bodies_2: list[dict] = []
        with patch(
            "httpx.post",
            side_effect=lambda url, *, headers, json, timeout, follow_redirects: (
                bodies_2.append(json) or MagicMock(status_code=200)
            ),
        ):
            ra.push_pending_enterprise_approvals(
                db_path=db_path, cloud_endpoint="https://cloud.example.com",
                instance_secret="s",
            )
        assert bodies_2[0]["request_id"] != request_id_1

        # The admin FINALLY resolves the STALE ticket #1 — arrives after #2 exists.
        stale_envelope = _build_envelope(
            proposal_id=proposal_id_str, action_digest="dig-stale",
            decision="approve", request_id=request_id_1,
        )
        stale_sig = _sign_envelope(private_key, stale_envelope)

        with patch("hermes.runtime.security_hook.signal_native_danger_approval") as mock_signal:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                ra._ensure_remote_approval_schema(conn)
                outcome = ra._verify_and_apply_decision(
                    item={**stale_envelope, "signature_hex": stale_sig},
                    pubkey_hex=pubkey_hex, own_instance_id=_OWN_INSTANCE_ID, conn=conn, db_path=db_path,
                )

        assert outcome == "stale_request"
        mock_signal.assert_not_called()
        assert _row(db_path, proposal_id_str)["status"] == "pending"  # occurrence #2 untouched
        assert "stale_request" in ra._ACK_OUTCOMES  # must not be re-served forever either


# ---------------------------------------------------------------------------
# run_remote_approvals_once — orchestration fail-safe posture
# ---------------------------------------------------------------------------


class TestRunRemoteApprovalsOnce:
    def test_unassociated_store_is_a_no_op(self) -> None:
        store = MagicMock()
        store.is_associated.return_value = False
        store.get.return_value = None

        with patch("httpx.post") as mock_post, patch("httpx.get") as mock_get:
            ra.run_remote_approvals_once(store=store)

        mock_post.assert_not_called()
        mock_get.assert_not_called()

    def test_unsafe_endpoint_is_a_no_op(self) -> None:
        assoc = MagicMock()
        assoc.cloud_endpoint = "http://not-https.example.com"  # rejected: not https
        assoc.signing_pubkey_hex = "a" * 64
        store = MagicMock()
        store.is_associated.return_value = True
        store.get.return_value = assoc

        with patch("httpx.post") as mock_post, patch("httpx.get") as mock_get:
            ra.run_remote_approvals_once(store=store)

        mock_post.assert_not_called()
        mock_get.assert_not_called()

    def test_invalid_pubkey_is_a_no_op(self) -> None:
        assoc = MagicMock()
        assoc.cloud_endpoint = "https://cloud.example.com"
        assoc.signing_pubkey_hex = "not-hex-and-wrong-length"
        store = MagicMock()
        store.is_associated.return_value = True
        store.get.return_value = assoc

        with patch("httpx.post") as mock_post, patch("httpx.get") as mock_get:
            ra.run_remote_approvals_once(store=store)

        mock_post.assert_not_called()
        mock_get.assert_not_called()

    def test_missing_instance_secret_is_a_no_op(self) -> None:
        assoc = MagicMock()
        assoc.cloud_endpoint = "https://cloud.example.com"
        assoc.signing_pubkey_hex = "a" * 64
        store = MagicMock()
        store.is_associated.return_value = True
        store.get.return_value = assoc
        store.reveal_instance_secret.return_value = None

        with patch("httpx.post") as mock_post, patch("httpx.get") as mock_get:
            ra.run_remote_approvals_once(store=store)

        mock_post.assert_not_called()
        mock_get.assert_not_called()
