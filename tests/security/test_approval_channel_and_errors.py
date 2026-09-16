"""Owner-channel regression after retiring Community MFA.

The old forwarding cases below now assert that factors are rejected or absent.
Generic structured-error decoding remains tested, including historical reasons.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.unit

_AUTHORIZED_UID = 1000
_SIGNING_KEY = os.urandom(32)


# ---------------------------------------------------------------------------
# Fake gate that records mfa_factors passed to approve
# ---------------------------------------------------------------------------


class _RecordingApprovalGate:
    """ApprovalGatePort fake that records mfa_factors forwarded to approve."""

    def __init__(self) -> None:
        self.approve_calls: list[dict] = []
        self.reject_calls: list[dict] = []

    async def register_pending(self, *, proposal_id, **_) -> None:
        pass

    async def approve(self, *, proposal_id: UUID, approved_by: UUID, mfa_factors=None) -> str:
        self.approve_calls.append({
            "proposal_id": proposal_id,
            "approved_by": approved_by,
            "mfa_factors": mfa_factors,
        })
        return f"fake-token-{proposal_id}"

    async def reject(self, *, proposal_id: UUID, rejected_by: UUID, reason: str) -> None:
        self.reject_calls.append({"proposal_id": proposal_id, "reason": reason})

    async def verify_token(self, *, proposal_id: UUID, token: str) -> bool:
        return True

    async def approved_token_for(self, proposal_id: UUID) -> str | None:
        return None

    async def work_item_id_for_proposal(self, proposal_id: UUID):
        return None


def _make_wiring_with_recording_gate():
    from hermes.agents_os.infrastructure.dbus_runtime_service import DbusRuntimeServiceWiring
    from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

    state = InMemoryAgentState(paused=False)
    gate = _RecordingApprovalGate()
    wiring = DbusRuntimeServiceWiring(
        agent_state=state,
        approval_gate=gate,
        authorized_uids=frozenset({_AUTHORIZED_UID}),
    )
    return wiring, gate


# ---------------------------------------------------------------------------
# Approval uses the verified owner channel, with no factor forwarding.
# ---------------------------------------------------------------------------


class TestApproveActionOwnerChannel:
    """Community no longer accepts or forwards TOTP."""

    async def test_removed_totp_argument_cannot_reach_gate(self) -> None:
        """No hidden compatibility path accepts a Community factor."""
        wiring, gate = _make_wiring_with_recording_gate()
        proposal_id = uuid4()
        with pytest.raises(TypeError):
            await wiring.approve_action(
                proposal_id=proposal_id, sender_uid=_AUTHORIZED_UID, totp="123456",
            )
        assert gate.approve_calls == []

    async def test_no_totp_gives_none_mfa_factors(self) -> None:
        """When totp is absent, gate.approve receives mfa_factors=None (simple-tier)."""
        wiring, gate = _make_wiring_with_recording_gate()
        proposal_id = uuid4()
        await wiring.approve_action(
            proposal_id=proposal_id,
            sender_uid=_AUTHORIZED_UID,
        )
        assert len(gate.approve_calls) == 1
        assert gate.approve_calls[0]["approved_by"] == UUID(int=_AUTHORIZED_UID)
        assert gate.approve_calls[0]["mfa_factors"] is None, (
            "Without totp, mfa_factors must be None so simple-tier tools pass through."
        )

    async def test_empty_string_totp_gives_none_mfa_factors(self) -> None:
        """An ordinary owner decision carries no MFA factors."""
        wiring, gate = _make_wiring_with_recording_gate()
        proposal_id = uuid4()
        await wiring.approve_action(
            proposal_id=proposal_id,
            sender_uid=_AUTHORIZED_UID,
        )
        assert len(gate.approve_calls) == 1
        assert gate.approve_calls[0]["mfa_factors"] is None


# ---------------------------------------------------------------------------
# Bug #2: _translate_dbus_error must extract real reason from structured name
# ---------------------------------------------------------------------------


class TestTranslateDbusErrorReasonExtraction:
    """_translate_dbus_error must not collapse all ApprovalGateError reasons to 'proposal_invalid'."""

    def _make_dbus_error(self, name: str, message: str):
        """Build a dbus_fast DBusError with a structured name."""
        from dbus_fast import DBusError
        return DBusError(name, message)

    def test_mfa_required_reason_preserved(self) -> None:
        """org.hermes.Error.ApprovalGate.mfa_required → reason='mfa_required', not 'proposal_invalid'."""
        from hermes.shell_server.chat.dbus_control_plane_adapter import _translate_dbus_error
        from hermes.capabilities.infrastructure.sqlite_approval_gate import ApprovalGateError

        exc = self._make_dbus_error(
            "org.hermes.Error.ApprovalGate.mfa_required",
            "MFA inválida para aprobar tool mfa-tier 'skill_manage' (motivo=mfa_required).",
        )
        with pytest.raises(ApprovalGateError) as exc_info:
            _translate_dbus_error(exc)
        assert exc_info.value.reason == "mfa_required", (
            "Previously _translate_dbus_error always set reason='proposal_invalid', masking "
            "the real MFA failure and making a valid TOTP approval look like a missing proposal."
        )

    def test_invalid_totp_reason_preserved(self) -> None:
        """org.hermes.Error.ApprovalGate.invalid_totp → reason='invalid_totp'."""
        from hermes.shell_server.chat.dbus_control_plane_adapter import _translate_dbus_error
        from hermes.capabilities.infrastructure.sqlite_approval_gate import ApprovalGateError

        exc = self._make_dbus_error(
            "org.hermes.Error.ApprovalGate.invalid_totp",
            "TOTP incorrecto.",
        )
        with pytest.raises(ApprovalGateError) as exc_info:
            _translate_dbus_error(exc)
        assert exc_info.value.reason == "invalid_totp"

    def test_mfa_not_enrolled_reason_preserved(self) -> None:
        """org.hermes.Error.ApprovalGate.mfa_not_enrolled → reason='mfa_not_enrolled'."""
        from hermes.shell_server.chat.dbus_control_plane_adapter import _translate_dbus_error
        from hermes.capabilities.infrastructure.sqlite_approval_gate import ApprovalGateError

        exc = self._make_dbus_error(
            "org.hermes.Error.ApprovalGate.mfa_not_enrolled",
            "MFA no enrolado.",
        )
        with pytest.raises(ApprovalGateError) as exc_info:
            _translate_dbus_error(exc)
        assert exc_info.value.reason == "mfa_not_enrolled"

    def test_real_proposal_invalid_reason_preserved(self) -> None:
        """org.hermes.Error.ApprovalGate.proposal_invalid → reason='proposal_invalid' (correct case)."""
        from hermes.shell_server.chat.dbus_control_plane_adapter import _translate_dbus_error
        from hermes.capabilities.infrastructure.sqlite_approval_gate import ApprovalGateError

        exc = self._make_dbus_error(
            "org.hermes.Error.ApprovalGate.proposal_invalid",
            "proposal_id=X no existe o ya fue resuelta.",
        )
        with pytest.raises(ApprovalGateError) as exc_info:
            _translate_dbus_error(exc)
        assert exc_info.value.reason == "proposal_invalid"

    def test_legacy_fallback_still_maps_to_proposal_invalid(self) -> None:
        """Unstructured 'ApprovalGateError' in message text → fallback to 'proposal_invalid'."""
        from hermes.shell_server.chat.dbus_control_plane_adapter import _translate_dbus_error
        from hermes.capabilities.infrastructure.sqlite_approval_gate import ApprovalGateError

        # Old daemon that raised an untyped error with class name in the message.
        exc = self._make_dbus_error(
            "org.hermes.Error.Unknown",
            "ApprovalGateError: proposal not found",
        )
        with pytest.raises(ApprovalGateError) as exc_info:
            _translate_dbus_error(exc)
        assert exc_info.value.reason == "proposal_invalid"
