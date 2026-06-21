"""spec 014 increment 3 — Tests for GrantConsent / RevokeConsent / ListConsents D-Bus verbs.

Rules verified (FR-013 / CWE-862 / CTRL-P1-2):
  1. grant_consent with authorized sender_uid creates an active consent.
  2. grant_consent with unauthorized sender_uid raises DbusAuthorizationError (fail-closed).
  3. human_operator_id is UUID(int=sender_uid) — resolved server-side, never from payload.
  4. grant_consent with invalid capability string returns {"error": ...} — no exception leaks.
  5. grant_consent with invalid scope string returns {"error": ...}.
  6. grant_consent session scope creates an expiring consent (expires_at set).
  7. grant_consent once scope creates a single-use consent.
  8. revoke_consent removes the active consent; list_consents returns empty.
  9. revoke_consent when no active consent returns {"revoked": False}.
 10. revoke_consent with unauthorized sender_uid raises DbusAuthorizationError.
 11. revoke_consent with invalid capability returns {"error": ...}.
 12. list_consents is read-only — returns JSON, no authZ guard.
 13. list_consents scoped to calling operator by sender_uid.
 14. grant_consent without consent_manager returns {"error": "consent_manager_not_configured"}.
 15. revoke_consent without consent_manager returns {"error": "consent_manager_not_configured"}.
 16. list_consents without consent_manager returns JSON empty list.
 17. After GrantConsent, the same ConsentManager object used by the broker has an active consent.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from hermes.agents_os.application.consent_manager import (
    Capability,
    ConsentManager,
    ConsentScope,
)
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000
_UNAUTHORIZED_UID = 9999
_OPERATOR_UUID = UUID(int=_OPERATOR_UID)


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


class _NullApprovalGate:
    async def approve(self, *, proposal_id, approved_by) -> str:
        return "tok"

    async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...

    async def verify_token(self, *, proposal_id, token) -> bool:
        return False

    async def approved_token_for(self, proposal_id) -> str | None:
        return None


def _make_wiring(*, consent_manager: ConsentManager | None = None) -> DbusRuntimeServiceWiring:
    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
        consent_manager=consent_manager,
    )


def _make_manager() -> ConsentManager:
    return ConsentManager()


# ---------------------------------------------------------------------------
# GrantConsent — happy path
# ---------------------------------------------------------------------------


class TestGrantConsent:
    def test_authorized_uid_creates_active_consent(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)

        result = wiring.grant_consent(
            capability="documents", scope="session", sender_uid=_OPERATOR_UID
        )

        assert "error" not in result
        assert result["capability"] == "documents"
        assert result["scope"] == "session"
        assert result["consent_id"]
        # The manager itself has the active consent.
        active = mgr.list_active(human_operator_id=_OPERATOR_UUID)
        assert len(active) == 1
        assert active[0].capability == Capability.DOCUMENTS

    def test_operator_id_is_derived_from_sender_uid_not_payload(self) -> None:
        """human_operator_id must equal UUID(int=sender_uid) — CWE-862."""
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        wiring.grant_consent(capability="documents", scope="session", sender_uid=_OPERATOR_UID)

        active = mgr.list_active(human_operator_id=_OPERATOR_UUID)
        assert len(active) == 1
        assert active[0].human_operator_id == _OPERATOR_UUID

    def test_session_scope_sets_expires_at(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        result = wiring.grant_consent(
            capability="documents", scope="session", sender_uid=_OPERATOR_UID
        )
        assert result["expires_at"] is not None

    def test_persistent_scope_no_expiry(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        result = wiring.grant_consent(
            capability="documents", scope="persistent", sender_uid=_OPERATOR_UID
        )
        assert result["expires_at"] is None

    def test_once_scope_accepted(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        result = wiring.grant_consent(
            capability="terminal", scope="once", sender_uid=_OPERATOR_UID
        )
        assert result["scope"] == "once"

    def test_all_valid_capabilities_accepted(self) -> None:
        for cap in Capability:
            mgr = _make_manager()
            wiring = _make_wiring(consent_manager=mgr)
            result = wiring.grant_consent(
                capability=cap.value, scope="session", sender_uid=_OPERATOR_UID
            )
            assert "error" not in result, f"capability {cap.value!r} should be valid"

    def test_result_is_json_serializable(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        result = wiring.grant_consent(
            capability="documents", scope="session", sender_uid=_OPERATOR_UID
        )
        # Must not raise.
        json.dumps(result)


# ---------------------------------------------------------------------------
# GrantConsent — security / error paths
# ---------------------------------------------------------------------------


class TestGrantConsentSecurity:
    def test_unauthorized_uid_raises_dbus_authz_error(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        with pytest.raises(DbusAuthorizationError):
            wiring.grant_consent(
                capability="documents", scope="session", sender_uid=_UNAUTHORIZED_UID
            )

    def test_unauthorized_uid_does_not_mutate_manager(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        try:
            wiring.grant_consent(
                capability="documents", scope="session", sender_uid=_UNAUTHORIZED_UID
            )
        except DbusAuthorizationError:
            pass
        assert len(mgr.list_active(human_operator_id=_OPERATOR_UUID)) == 0

    def test_invalid_capability_returns_error_dict(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        result = wiring.grant_consent(
            capability="NONEXISTENT_CAP", scope="session", sender_uid=_OPERATOR_UID
        )
        assert "error" in result
        assert "NONEXISTENT_CAP" in result["error"]

    def test_invalid_scope_returns_error_dict(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        result = wiring.grant_consent(
            capability="documents", scope="INVALID_SCOPE", sender_uid=_OPERATOR_UID
        )
        assert "error" in result
        assert "INVALID_SCOPE" in result["error"]

    def test_no_consent_manager_returns_error_dict(self) -> None:
        wiring = _make_wiring(consent_manager=None)
        result = wiring.grant_consent(
            capability="documents", scope="session", sender_uid=_OPERATOR_UID
        )
        assert result == {"error": "consent_manager_not_configured"}


# ---------------------------------------------------------------------------
# RevokeConsent
# ---------------------------------------------------------------------------


class TestRevokeConsent:
    def test_revoke_active_consent_returns_revoked_true(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        wiring.grant_consent(capability="documents", scope="session", sender_uid=_OPERATOR_UID)

        result = wiring.revoke_consent(capability="documents", sender_uid=_OPERATOR_UID)

        assert result["revoked"] is True

    def test_revoke_removes_consent_from_manager(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        wiring.grant_consent(capability="documents", scope="session", sender_uid=_OPERATOR_UID)
        wiring.revoke_consent(capability="documents", sender_uid=_OPERATOR_UID)

        assert len(mgr.list_active(human_operator_id=_OPERATOR_UUID)) == 0

    def test_revoke_non_existent_returns_revoked_false(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        result = wiring.revoke_consent(capability="terminal", sender_uid=_OPERATOR_UID)
        assert result == {"revoked": False}

    def test_unauthorized_uid_raises(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        with pytest.raises(DbusAuthorizationError):
            wiring.revoke_consent(capability="documents", sender_uid=_UNAUTHORIZED_UID)

    def test_invalid_capability_returns_error_dict(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        result = wiring.revoke_consent(capability="BOGUS", sender_uid=_OPERATOR_UID)
        assert "error" in result

    def test_no_consent_manager_returns_error_dict(self) -> None:
        wiring = _make_wiring(consent_manager=None)
        result = wiring.revoke_consent(capability="documents", sender_uid=_OPERATOR_UID)
        assert result == {"error": "consent_manager_not_configured"}

    def test_result_is_json_serializable(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        wiring.grant_consent(capability="documents", scope="session", sender_uid=_OPERATOR_UID)
        result = wiring.revoke_consent(capability="documents", sender_uid=_OPERATOR_UID)
        json.dumps(result)


# ---------------------------------------------------------------------------
# ListConsents
# ---------------------------------------------------------------------------


class TestListConsents:
    def test_returns_empty_json_when_no_active_consents(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        raw = wiring.list_consents(sender_uid=_OPERATOR_UID)
        assert json.loads(raw) == []

    def test_returns_granted_consent(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        wiring.grant_consent(capability="documents", scope="session", sender_uid=_OPERATOR_UID)

        raw = wiring.list_consents(sender_uid=_OPERATOR_UID)
        items = json.loads(raw)

        assert len(items) == 1
        assert items[0]["capability"] == "documents"

    def test_scoped_to_calling_operator(self) -> None:
        """ListConsents must only show consents of the calling operator."""
        mgr = _make_manager()
        # Grant consent for operator 1000.
        mgr.grant(
            tenant_id=UUID(int=0),
            human_operator_id=_OPERATOR_UUID,
            capability=Capability.DOCUMENTS,
            scope=ConsentScope.SESSION,
        )
        # Grant consent for a different operator (e.g. uid 2000).
        other_uuid = UUID(int=2000)
        mgr.grant(
            tenant_id=UUID(int=0),
            human_operator_id=other_uuid,
            capability=Capability.TERMINAL,
            scope=ConsentScope.SESSION,
        )
        wiring = _make_wiring(consent_manager=mgr)

        raw = wiring.list_consents(sender_uid=_OPERATOR_UID)
        items = json.loads(raw)

        # Only operator 1000's consents returned.
        assert len(items) == 1
        assert items[0]["capability"] == "documents"

    def test_read_only_any_uid_succeeds(self) -> None:
        """ListConsents is read-only — no authZ guard (same policy as list_*)."""
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        # Even unauthorized UID must succeed (read-only).
        raw = wiring.list_consents(sender_uid=_UNAUTHORIZED_UID)
        assert isinstance(json.loads(raw), list)

    def test_no_consent_manager_returns_empty_list(self) -> None:
        wiring = _make_wiring(consent_manager=None)
        raw = wiring.list_consents(sender_uid=_OPERATOR_UID)
        assert json.loads(raw) == []

    def test_revoked_consent_not_listed(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        wiring.grant_consent(capability="documents", scope="session", sender_uid=_OPERATOR_UID)
        wiring.revoke_consent(capability="documents", sender_uid=_OPERATOR_UID)

        raw = wiring.list_consents(sender_uid=_OPERATOR_UID)
        assert json.loads(raw) == []

    def test_result_is_json_parseable(self) -> None:
        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        wiring.grant_consent(capability="documents", scope="session", sender_uid=_OPERATOR_UID)
        raw = wiring.list_consents(sender_uid=_OPERATOR_UID)
        items = json.loads(raw)
        assert isinstance(items, list)
        assert all(isinstance(i, dict) for i in items)


# ---------------------------------------------------------------------------
# Integration invariant: shared ConsentManager (broker gate regression)
# ---------------------------------------------------------------------------


class TestConsentManagerSharedWithBroker:
    """The SAME ConsentManager injected into the wiring must be the one the broker
    uses for assert_active(). This is the critical invariant: GrantConsent via
    D-Bus must make the consent immediately visible to the capability broker
    without any intermediate sync step.

    This test verifies that grant_consent() mutates the in-memory manager in
    place, so a subsequent assert_active() on the same object succeeds.
    """

    def test_grant_then_assert_active_succeeds(self) -> None:
        from hermes.agents_os.application.consent_manager import ConsentDenied  # noqa: PLC0415

        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)

        # Before grant → broker would deny.
        with pytest.raises(ConsentDenied):
            mgr.assert_active(
                human_operator_id=_OPERATOR_UUID, capability=Capability.DOCUMENTS
            )

        # Operator grants via D-Bus.
        result = wiring.grant_consent(
            capability="documents", scope="session", sender_uid=_OPERATOR_UID
        )
        assert "error" not in result

        # After grant → broker assertion passes (same mgr object, no sync needed).
        consent = mgr.assert_active(
            human_operator_id=_OPERATOR_UUID, capability=Capability.DOCUMENTS
        )
        assert consent.capability == Capability.DOCUMENTS

    def test_revoke_then_assert_active_raises(self) -> None:
        from hermes.agents_os.application.consent_manager import ConsentDenied  # noqa: PLC0415

        mgr = _make_manager()
        wiring = _make_wiring(consent_manager=mgr)
        wiring.grant_consent(capability="documents", scope="session", sender_uid=_OPERATOR_UID)
        wiring.revoke_consent(capability="documents", sender_uid=_OPERATOR_UID)

        with pytest.raises(ConsentDenied):
            mgr.assert_active(
                human_operator_id=_OPERATOR_UUID, capability=Capability.DOCUMENTS
            )
