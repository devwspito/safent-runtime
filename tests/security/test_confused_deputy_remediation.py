"""Confused-deputy remediation tests (ALTO security finding).

Verifies the hybrid model:
  (a) Mutator from proxy uid WITHOUT token → denied (DbusAuthorizationError).
  (b) Mutator from proxy uid WITH valid token → authorized, attributed to
      the operator from the token, NOT to the proxy uid.
  (c) Token expired / forged / tampered → denied.
  (d) Direct mutator from authorized operator uid → still works, attributed
      to that operator uid.
  (e) Read-only method without token → OK (no authZ required).

Also covers OperatorToken unit tests:
  - mint() produces a verifiable token.
  - verify() with wrong key → OperatorTokenForged.
  - verify() with expired token → OperatorTokenExpired.
  - verify() with wrong operation → OperatorTokenError.
  - hmac.compare_digest prevents timing attacks (presence check only).
"""

from __future__ import annotations

import os
import time
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
    HitlApprovalResult,
)
from hermes.shell_server.security.operator_token import (
    OperatorTokenExpired,
    OperatorTokenForged,
    OperatorTokenMalformed,
    OperatorTokenMinter,
    OperatorTokenVerifier,
)
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.security

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AUTHORIZED_UID = 1000          # hermes-user (direct operator)
_PROXY_UID = 880                # hermes process (shell-server)
_UNAUTHORIZED_UID = 9999        # third process — not in authorized_uids
_OPERATOR_UUID = uuid4()        # stable operator UUID for token claims
_SIGNING_KEY = os.urandom(32)
_WRONG_KEY = os.urandom(32)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeApprovalGate:
    def __init__(self) -> None:
        self.approve_calls: list[dict] = []
        self.reject_calls: list[dict] = []

    async def approve(self, *, proposal_id: UUID, approved_by: UUID) -> str:
        self.approve_calls.append(
            {"proposal_id": proposal_id, "approved_by": approved_by}
        )
        return f"token-{proposal_id}"

    async def reject(self, *, proposal_id: UUID, rejected_by: UUID, reason: str) -> None:
        self.reject_calls.append(
            {"proposal_id": proposal_id, "rejected_by": rejected_by, "reason": reason}
        )


def _make_minter_verifier(
    key: bytes = _SIGNING_KEY, expiry_s: int = 30
) -> tuple[OperatorTokenMinter, OperatorTokenVerifier]:
    minter = OperatorTokenMinter(signing_key=key, expiry_s=expiry_s)
    verifier = OperatorTokenVerifier(signing_key=key)
    return minter, verifier


def _make_wiring(
    *,
    minter: OperatorTokenMinter | None = None,
    verifier: OperatorTokenVerifier | None = None,
    include_proxy: bool = True,
    paused: bool = False,
) -> tuple[DbusRuntimeServiceWiring, InMemoryAgentState, _FakeApprovalGate]:
    state = InMemoryAgentState(paused=paused)
    gate = _FakeApprovalGate()
    if minter is None and verifier is None and include_proxy:
        minter, verifier = _make_minter_verifier()
    wiring = DbusRuntimeServiceWiring(
        agent_state=state,
        approval_gate=gate,
        authorized_uids=frozenset({_AUTHORIZED_UID}),
        proxy_uid=_PROXY_UID if include_proxy else None,
        operator_token_verifier=verifier,
    )
    return wiring, state, gate


def _mint_token(minter: OperatorTokenMinter, *, operation: str) -> str:
    return minter.mint(operator_id=str(_OPERATOR_UUID), operation=operation)


# ============================================================================
# (a) Proxy uid WITHOUT token → denied
# ============================================================================


class TestProxyWithoutTokenDenied:
    """Proxy uid (shell-server process) without operator token is always denied."""

    async def test_proxy_pause_no_token_denied(self) -> None:
        wiring, state, _ = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(
                reason="test", sender_uid=_PROXY_UID, operator_token=None
            )
        assert await state.is_paused() is False, "State must not change on denial"

    async def test_proxy_resume_no_token_denied(self) -> None:
        wiring, state, _ = _make_wiring(paused=True)
        with pytest.raises(DbusAuthorizationError):
            await wiring.request_resume(sender_uid=_PROXY_UID, operator_token=None)
        assert await state.is_paused() is True, "State must remain paused on denial"

    async def test_proxy_approve_no_token_denied(self) -> None:
        wiring, _, gate = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.approve_action(
                proposal_id=uuid4(), sender_uid=_PROXY_UID, operator_token=None
            )
        assert len(gate.approve_calls) == 0, "Gate must not be called on denial"

    async def test_proxy_reject_no_token_denied(self) -> None:
        wiring, _, gate = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.reject_action(
                proposal_id=uuid4(),
                reason="test",
                sender_uid=_PROXY_UID,
                operator_token=None,
            )
        assert len(gate.reject_calls) == 0

    async def test_fully_unauthorized_uid_still_denied(self) -> None:
        """A uid that is neither an operator nor the proxy is always denied."""
        wiring, state, _ = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(
                reason="test",
                sender_uid=_UNAUTHORIZED_UID,
                operator_token=None,
            )


# ============================================================================
# (b) Proxy uid WITH valid token → authorized, attributed to token operator
# ============================================================================


class TestProxyWithValidTokenAuthorized:
    """Proxy uid + valid token → authorized, attribution = operator from token."""

    async def test_proxy_pause_with_token_authorized(self) -> None:
        minter, verifier = _make_minter_verifier()
        wiring, state, _ = _make_wiring(minter=minter, verifier=verifier)
        token = _mint_token(minter, operation="request_pause")

        await wiring.request_pause(
            reason="operator via proxy", sender_uid=_PROXY_UID, operator_token=token
        )

        assert await state.is_paused() is True
        assert len(state.pause_calls) == 1
        assert state.pause_calls[0]["by"] == _OPERATOR_UUID, (
            "Attribution must be the operator from the token, not proxy uid"
        )

    async def test_proxy_resume_with_token_authorized(self) -> None:
        minter, verifier = _make_minter_verifier()
        wiring, state, _ = _make_wiring(minter=minter, verifier=verifier, paused=True)
        token = _mint_token(minter, operation="request_resume")

        await wiring.request_resume(sender_uid=_PROXY_UID, operator_token=token)

        assert await state.is_paused() is False
        assert state.resume_calls[0]["by"] == _OPERATOR_UUID

    async def test_proxy_approve_with_token_authorized_and_attributed(self) -> None:
        minter, verifier = _make_minter_verifier()
        wiring, _, gate = _make_wiring(minter=minter, verifier=verifier)
        token = _mint_token(minter, operation="approve_action")
        proposal_id = uuid4()

        result = await wiring.approve_action(
            proposal_id=proposal_id,
            sender_uid=_PROXY_UID,
            operator_token=token,
        )

        assert isinstance(result, HitlApprovalResult)
        assert result.approved_by == _OPERATOR_UUID, (
            "approved_by must be operator from token, not proxy uid"
        )
        assert gate.approve_calls[0]["approved_by"] == _OPERATOR_UUID

    async def test_proxy_reject_with_token_authorized_and_attributed(self) -> None:
        minter, verifier = _make_minter_verifier()
        wiring, _, gate = _make_wiring(minter=minter, verifier=verifier)
        token = _mint_token(minter, operation="reject_action")
        proposal_id = uuid4()

        await wiring.reject_action(
            proposal_id=proposal_id,
            reason="proxy reject",
            sender_uid=_PROXY_UID,
            operator_token=token,
        )

        assert gate.reject_calls[0]["rejected_by"] == _OPERATOR_UUID

    async def test_proxy_uid_never_appears_as_attribution(self) -> None:
        """The proxy process uid must never be in any attribution field."""
        minter, verifier = _make_minter_verifier()
        wiring, state, _ = _make_wiring(minter=minter, verifier=verifier)
        token = _mint_token(minter, operation="request_pause")

        await wiring.request_pause(
            reason="check attribution",
            sender_uid=_PROXY_UID,
            operator_token=token,
        )

        proxy_as_uuid = UUID(int=_PROXY_UID)
        assert state.pause_calls[0]["by"] != proxy_as_uuid, (
            "Proxy uid must never appear as attribution"
        )


# ============================================================================
# (c) Token expired / forged → denied
# ============================================================================


class TestInvalidTokenDenied:
    """Expired, forged, or malformed tokens are always rejected (fail-closed)."""

    async def test_expired_token_denied(self) -> None:
        minter, verifier = _make_minter_verifier(expiry_s=1)
        wiring, state, _ = _make_wiring(minter=minter, verifier=verifier)
        token = _mint_token(minter, operation="request_pause")

        # Wait for the token to expire.
        time.sleep(2)

        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(
                reason="expired", sender_uid=_PROXY_UID, operator_token=token
            )
        assert await state.is_paused() is False

    async def test_forged_token_denied(self) -> None:
        """Token signed with a different key is rejected."""
        wrong_minter, _ = _make_minter_verifier(key=_WRONG_KEY)
        _, good_verifier = _make_minter_verifier(key=_SIGNING_KEY)
        wiring, state, _ = _make_wiring(verifier=good_verifier)
        forged_token = _mint_token(wrong_minter, operation="request_pause")

        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(
                reason="forged", sender_uid=_PROXY_UID, operator_token=forged_token
            )
        assert await state.is_paused() is False

    async def test_tampered_token_denied(self) -> None:
        """Token with manually altered payload is rejected."""
        minter, verifier = _make_minter_verifier()
        wiring, state, _ = _make_wiring(minter=minter, verifier=verifier)
        token = _mint_token(minter, operation="request_pause")

        # Alter the operator_id field (first segment) to a different UUID.
        parts = token.split("|")
        parts[0] = str(uuid4())  # replace operator_id
        tampered = "|".join(parts)

        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(
                reason="tampered", sender_uid=_PROXY_UID, operator_token=tampered
            )

    async def test_wrong_operation_token_denied(self) -> None:
        """Token minted for 'approve_action' cannot be used for 'request_pause'."""
        minter, verifier = _make_minter_verifier()
        wiring, state, _ = _make_wiring(minter=minter, verifier=verifier)
        token = _mint_token(minter, operation="approve_action")

        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(
                reason="wrong-op", sender_uid=_PROXY_UID, operator_token=token
            )
        assert await state.is_paused() is False

    async def test_malformed_token_denied(self) -> None:
        """Non-parseable token string is rejected without executing the operation."""
        minter, verifier = _make_minter_verifier()
        wiring, state, _ = _make_wiring(minter=minter, verifier=verifier)

        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(
                reason="malformed",
                sender_uid=_PROXY_UID,
                operator_token="not-a-valid-token",
            )

    async def test_no_verifier_configured_proxy_denied(self) -> None:
        """proxy_uid set but no token_verifier → any proxy call denied."""
        state = InMemoryAgentState()
        gate = _FakeApprovalGate()
        wiring = DbusRuntimeServiceWiring(
            agent_state=state,
            approval_gate=gate,
            authorized_uids=frozenset({_AUTHORIZED_UID}),
            proxy_uid=_PROXY_UID,
            operator_token_verifier=None,  # not configured
        )
        minter = OperatorTokenMinter(signing_key=_SIGNING_KEY)
        token = minter.mint(operator_id=str(_OPERATOR_UUID), operation="request_pause")

        with pytest.raises(DbusAuthorizationError):
            await wiring.request_pause(
                reason="no-verifier", sender_uid=_PROXY_UID, operator_token=token
            )


# ============================================================================
# (d) Direct operator uid → still works, attributed to operator uid
# ============================================================================


class TestDirectOperatorUnchanged:
    """Direct operator calls (uid in authorized_uids) continue to work correctly."""

    async def test_direct_operator_can_pause(self) -> None:
        wiring, state, _ = _make_wiring()
        await wiring.request_pause(reason="direct", sender_uid=_AUTHORIZED_UID)
        assert await state.is_paused() is True

    async def test_direct_operator_attribution_is_uid(self) -> None:
        wiring, state, _ = _make_wiring()
        await wiring.request_pause(reason="direct", sender_uid=_AUTHORIZED_UID)
        expected = UUID(int=_AUTHORIZED_UID)
        assert state.pause_calls[0]["by"] == expected

    async def test_direct_operator_no_token_needed(self) -> None:
        """Direct operator call succeeds even without an operator_token."""
        wiring, state, _ = _make_wiring()
        await wiring.request_pause(
            reason="no-token-needed",
            sender_uid=_AUTHORIZED_UID,
            operator_token=None,
        )
        assert await state.is_paused() is True

    async def test_direct_operator_approve_attributed_correctly(self) -> None:
        wiring, _, gate = _make_wiring()
        proposal_id = uuid4()
        result = await wiring.approve_action(
            proposal_id=proposal_id, sender_uid=_AUTHORIZED_UID
        )
        expected = UUID(int=_AUTHORIZED_UID)
        assert result.approved_by == expected
        assert gate.approve_calls[0]["approved_by"] == expected

    async def test_direct_operator_reject_attributed_correctly(self) -> None:
        wiring, _, gate = _make_wiring()
        await wiring.reject_action(
            proposal_id=uuid4(),
            reason="veto",
            sender_uid=_AUTHORIZED_UID,
        )
        expected = UUID(int=_AUTHORIZED_UID)
        assert gate.reject_calls[0]["rejected_by"] == expected


# ============================================================================
# (e) Read-only methods → no token required
# ============================================================================


class TestReadOnlyNoAuthRequired:
    """Read-only supervision methods do not require a token or authZ."""

    async def test_list_agents_no_auth_needed(self) -> None:
        wiring, _, _ = _make_wiring(include_proxy=False)
        result = wiring.list_agents()
        assert isinstance(result, list)

    async def test_list_skills_no_auth_needed(self) -> None:
        wiring, _, _ = _make_wiring(include_proxy=False)
        result = wiring.list_skills()
        assert isinstance(result, list)

    async def test_get_active_agent_no_auth_needed(self) -> None:
        wiring, _, _ = _make_wiring(include_proxy=False)
        result = wiring.get_active_agent()
        assert isinstance(result, str)

    async def test_list_pending_no_auth_needed(self) -> None:
        """list_pending returns empty list when cp_service not injected — no crash."""
        wiring, _, _ = _make_wiring(include_proxy=False)
        result = await wiring.list_pending(limit=10)
        assert isinstance(result, list)


# ============================================================================
# OperatorToken unit tests
# ============================================================================


class TestOperatorTokenMinterVerifier:
    """Unit tests for the OperatorToken building blocks."""

    def test_mint_and_verify_roundtrip(self) -> None:
        minter, verifier = _make_minter_verifier()
        token = minter.mint(operator_id=str(_OPERATOR_UUID), operation="request_pause")
        claims = verifier.verify(token, expected_operation="request_pause")
        assert claims.operator_id == str(_OPERATOR_UUID)
        assert claims.operation == "request_pause"

    def test_verify_wrong_key_raises_forged(self) -> None:
        minter, _ = _make_minter_verifier(key=_SIGNING_KEY)
        _, wrong_verifier = _make_minter_verifier(key=_WRONG_KEY)
        token = minter.mint(operator_id=str(_OPERATOR_UUID), operation="enqueue")
        with pytest.raises(OperatorTokenForged):
            wrong_verifier.verify(token)

    def test_verify_expired_raises_expired(self) -> None:
        minter, verifier = _make_minter_verifier(expiry_s=1)
        token = minter.mint(operator_id=str(_OPERATOR_UUID), operation="enqueue")
        time.sleep(2)
        with pytest.raises(OperatorTokenExpired):
            verifier.verify(token)

    def test_verify_wrong_operation_raises_error(self) -> None:
        minter, verifier = _make_minter_verifier()
        token = minter.mint(operator_id=str(_OPERATOR_UUID), operation="approve_action")
        from hermes.shell_server.security.operator_token import OperatorTokenError
        with pytest.raises(OperatorTokenError):
            verifier.verify(token, expected_operation="request_pause")

    def test_verify_malformed_raises_malformed(self) -> None:
        _, verifier = _make_minter_verifier()
        with pytest.raises(OperatorTokenMalformed):
            verifier.verify("too|few|fields")

    def test_tampered_sig_raises_forged(self) -> None:
        minter, verifier = _make_minter_verifier()
        token = minter.mint(operator_id=str(_OPERATOR_UUID), operation="enqueue")
        parts = token.split("|")
        parts[-1] = "a" * len(parts[-1])  # replace HMAC with garbage
        tampered = "|".join(parts)
        with pytest.raises(OperatorTokenForged):
            verifier.verify(tampered)

    def test_subkey_from_secrets_vault(self, tmp_path) -> None:
        """Token key derived from SecretsVault.derive_subkey is stable."""
        from hermes.shell_server.security.secrets import SecretsVault

        master = os.urandom(32)
        vault = SecretsVault(master_key=master)
        key1 = vault.derive_subkey(label="operator-token")
        key2 = vault.derive_subkey(label="operator-token")
        assert key1 == key2, "derive_subkey must be deterministic"

        minter = OperatorTokenMinter(signing_key=key1)
        verifier = OperatorTokenVerifier(signing_key=key2)
        token = minter.mint(operator_id=str(_OPERATOR_UUID), operation="enqueue")
        claims = verifier.verify(token, expected_operation="enqueue")
        assert claims.operator_id == str(_OPERATOR_UUID)


# ============================================================================
# P1 — CWE-441 Approve/Reject confused-deputy regression (2026-06-06)
#
# The D-Bus policy (org.hermes.Runtime1.conf) now DENIES Approve/Reject for
# user="hermes" (the daemon/shell-server process).  These tests verify the
# SOFTWARE-LAYER enforcement: the wiring's _authorize_and_resolve() already
# rejects a proxy (user=hermes) calling Approve/Reject without a valid
# operator_token — no change needed there.  What changed at policy layer is
# that the bus itself no longer lets user=hermes SEND those method calls at
# all.  The tests below are the software-layer contract that underpins the
# policy-layer fix: a compromised shell-server cannot self-approve HITL even
# if the D-Bus policy were relaxed.
# ============================================================================


class TestApproveRejectDeniedForProxyWithoutToken:
    """P1 regression: proxy uid MUST NOT approve/reject HITL without operator token.

    This is the confused-deputy gate at the software layer.  The D-Bus policy
    is the hardware-layer control (deny for user=hermes); this suite covers
    the software-layer to detect any regression in _authorize_and_resolve().
    """

    async def test_proxy_approve_without_token_denied(self) -> None:
        """user=hermes without operator_token cannot call Approve (CWE-441)."""
        wiring, _, gate = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.approve_action(
                proposal_id=uuid4(),
                sender_uid=_PROXY_UID,
                operator_token=None,
            )
        assert len(gate.approve_calls) == 0, (
            "Approve gate MUST NOT be called when authorization fails"
        )

    async def test_proxy_reject_without_token_denied(self) -> None:
        """user=hermes without operator_token cannot call Reject (CWE-441)."""
        wiring, _, gate = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.reject_action(
                proposal_id=uuid4(),
                reason="self-approval attempt",
                sender_uid=_PROXY_UID,
                operator_token=None,
            )
        assert len(gate.reject_calls) == 0, (
            "Reject gate MUST NOT be called when authorization fails"
        )

    async def test_approved_by_is_operator_uuid_not_proxy_uid(self) -> None:
        """When a valid token is present, approved_by MUST be the operator UUID.

        Even with a valid token, the attribution must resolve to the operator
        identity from the token — never to the proxy uid.  This closes the
        audit-chain confused-deputy gap where approved_by = proxy uid.
        """
        minter, verifier = _make_minter_verifier()
        wiring, _, gate = _make_wiring(minter=minter, verifier=verifier)

        token = _mint_token(minter, operation="approve_action")
        result = await wiring.approve_action(
            proposal_id=uuid4(),
            sender_uid=_PROXY_UID,
            operator_token=token,
        )
        assert isinstance(result, HitlApprovalResult)
        assert len(gate.approve_calls) == 1
        approved_by = gate.approve_calls[0]["approved_by"]
        # Must be the operator UUID from the token, NOT the proxy uid.
        assert approved_by == _OPERATOR_UUID, (
            f"approved_by must be the human operator UUID ({_OPERATOR_UUID}), "
            f"got {approved_by} — possible confused-deputy in attribution"
        )

    async def test_rejected_by_is_operator_uuid_not_proxy_uid(self) -> None:
        """Reject attribution must be the operator UUID, never the proxy uid."""
        minter, verifier = _make_minter_verifier()
        wiring, _, gate = _make_wiring(minter=minter, verifier=verifier)

        token = _mint_token(minter, operation="reject_action")
        await wiring.reject_action(
            proposal_id=uuid4(),
            reason="rejected by operator",
            sender_uid=_PROXY_UID,
            operator_token=token,
        )
        assert len(gate.reject_calls) == 1
        rejected_by = gate.reject_calls[0]["rejected_by"]
        assert rejected_by == _OPERATOR_UUID, (
            f"rejected_by must be the human operator UUID ({_OPERATOR_UUID}), "
            f"got {rejected_by}"
        )

    async def test_direct_operator_approve_does_not_require_token(self) -> None:
        """The direct operator path (hermes-user, uid=1000) must still work."""
        wiring, _, gate = _make_wiring()
        await wiring.approve_action(
            proposal_id=uuid4(),
            sender_uid=_AUTHORIZED_UID,
            operator_token=None,
        )
        assert len(gate.approve_calls) == 1
        # Attribution = operator UUID derived from their uid.
        approved_by = gate.approve_calls[0]["approved_by"]
        assert approved_by is not None

    async def test_direct_operator_reject_does_not_require_token(self) -> None:
        """The direct operator path (hermes-user) must still work for Reject."""
        wiring, _, gate = _make_wiring()
        await wiring.reject_action(
            proposal_id=uuid4(),
            reason="operator says no",
            sender_uid=_AUTHORIZED_UID,
            operator_token=None,
        )
        assert len(gate.reject_calls) == 1
