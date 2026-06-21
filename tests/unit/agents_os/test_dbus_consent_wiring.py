"""spec 014 increment 3 — FR-013 D-Bus consent wiring tests.

Verifies:
  - grant_consent: authorized sender_uid → ConsentManager.grant() called with
    human_operator_id = UUID(int=sender_uid), never from payload (CWE-862).
  - revoke_consent: authorized sender_uid → consent revoked.
  - list_consents: scoped to calling operator (sender_uid → human_operator_id).
  - Unauthorized sender_uid → DbusAuthorizationError, no state change (fail-closed).
  - grant_consent with invalid capability / scope → error dict (no exception).
  - ConsentManager is the SAME instance as the broker would use: grant via D-Bus
    makes assert_active() pass for the same operator and capability.
  - After revoke, assert_active() raises ConsentDenied again (gate restored).

These are pure unit tests — no D-Bus bus, no SQLite, no broker.
The ConsentManager uses in-memory mode (repo=None).
"""
from __future__ import annotations

from uuid import UUID

import pytest

from hermes.agents_os.application.consent_manager import (
    Capability,
    ConsentDenied,
    ConsentManager,
    ConsentScope,
)
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_AUTHORIZED_UID = 1000
_UNAUTHORIZED_UID = 9999


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeApprovalGate:
    async def approve(self, *, proposal_id, approved_by) -> str:
        return f"tok-{proposal_id}"

    async def reject(self, *, proposal_id, rejected_by, reason) -> None:
        pass


def _make_wiring(
    consent_manager: ConsentManager | None = None,
) -> tuple[DbusRuntimeServiceWiring, ConsentManager]:
    """Return (wiring, consent_manager) sharing the same ConsentManager instance."""
    cm = consent_manager if consent_manager is not None else ConsentManager()
    wiring = DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_FakeApprovalGate(),
        authorized_uids=frozenset({_AUTHORIZED_UID}),
        consent_manager=cm,
    )
    return wiring, cm


# ---------------------------------------------------------------------------
# grant_consent
# ---------------------------------------------------------------------------


class TestGrantConsent:
    def test_authorized_uid_grants_consent(self) -> None:
        wiring, cm = _make_wiring()
        result = wiring.grant_consent(
            capability="documents",
            scope="session",
            sender_uid=_AUTHORIZED_UID,
        )
        assert "error" not in result
        assert result["capability"] == "documents"
        assert result["scope"] == "session"

    def test_operator_id_is_derived_from_sender_uid_not_payload(self) -> None:
        """human_operator_id = UUID(int=sender_uid) — CWE-862 server-side."""
        wiring, cm = _make_wiring()
        wiring.grant_consent(
            capability="terminal",
            scope="session",
            sender_uid=_AUTHORIZED_UID,
        )
        expected_operator = UUID(int=_AUTHORIZED_UID)
        consents = cm.list_active(human_operator_id=expected_operator)
        assert len(consents) == 1
        assert consents[0].capability == Capability.TERMINAL
        assert consents[0].human_operator_id == expected_operator

    def test_unauthorized_uid_raises_and_no_state_change(self) -> None:
        wiring, cm = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            wiring.grant_consent(
                capability="documents",
                scope="session",
                sender_uid=_UNAUTHORIZED_UID,
            )
        # ConsentManager must not have been mutated.
        consents = cm.list_active(human_operator_id=UUID(int=_UNAUTHORIZED_UID))
        assert len(consents) == 0

    def test_invalid_capability_returns_error_dict(self) -> None:
        wiring, _ = _make_wiring()
        result = wiring.grant_consent(
            capability="not_a_real_capability",
            scope="session",
            sender_uid=_AUTHORIZED_UID,
        )
        assert "error" in result
        assert "capability" in result["error"].lower() or "inválida" in result["error"].lower()

    def test_invalid_scope_returns_error_dict(self) -> None:
        wiring, _ = _make_wiring()
        result = wiring.grant_consent(
            capability="documents",
            scope="invalid_scope",
            sender_uid=_AUTHORIZED_UID,
        )
        assert "error" in result
        assert "scope" in result["error"].lower() or "inválido" in result["error"].lower()

    def test_no_consent_manager_returns_not_configured(self) -> None:
        wiring = DbusRuntimeServiceWiring(
            agent_state=InMemoryAgentState(),
            approval_gate=_FakeApprovalGate(),
            authorized_uids=frozenset({_AUTHORIZED_UID}),
            consent_manager=None,
        )
        result = wiring.grant_consent(
            capability="documents",
            scope="session",
            sender_uid=_AUTHORIZED_UID,
        )
        assert "error" in result
        assert "not_configured" in result["error"]


# ---------------------------------------------------------------------------
# revoke_consent
# ---------------------------------------------------------------------------


class TestRevokeConsent:
    def test_authorized_uid_revokes_consent(self) -> None:
        cm = ConsentManager()
        op_id = UUID(int=_AUTHORIZED_UID)
        cm.grant(
            tenant_id=UUID(int=1),
            human_operator_id=op_id,
            capability=Capability.DOCUMENTS,
            scope=ConsentScope.SESSION,
        )
        wiring, _ = _make_wiring(consent_manager=cm)
        result = wiring.revoke_consent(
            capability="documents",
            sender_uid=_AUTHORIZED_UID,
        )
        assert result.get("revoked") is True
        # Confirm consent is gone from the manager.
        consents = cm.list_active(human_operator_id=op_id)
        assert all(c.capability != Capability.DOCUMENTS for c in consents)

    def test_revoke_non_existent_returns_revoked_false(self) -> None:
        wiring, _ = _make_wiring()
        result = wiring.revoke_consent(
            capability="documents",
            sender_uid=_AUTHORIZED_UID,
        )
        assert result.get("revoked") is False

    def test_unauthorized_uid_cannot_revoke(self) -> None:
        cm = ConsentManager()
        op_id = UUID(int=_AUTHORIZED_UID)
        cm.grant(
            tenant_id=UUID(int=1),
            human_operator_id=op_id,
            capability=Capability.DOCUMENTS,
            scope=ConsentScope.SESSION,
        )
        wiring, _ = _make_wiring(consent_manager=cm)
        with pytest.raises(DbusAuthorizationError):
            wiring.revoke_consent(
                capability="documents",
                sender_uid=_UNAUTHORIZED_UID,
            )
        # Consent must still be active.
        consents = cm.list_active(human_operator_id=op_id)
        assert any(c.capability == Capability.DOCUMENTS for c in consents)


# ---------------------------------------------------------------------------
# list_consents
# ---------------------------------------------------------------------------


class TestListConsents:
    def test_empty_when_no_consents(self) -> None:
        wiring, _ = _make_wiring()
        import json
        raw = wiring.list_consents(sender_uid=_AUTHORIZED_UID)
        assert json.loads(raw) == []

    def test_returns_only_active_consents_for_caller(self) -> None:
        cm = ConsentManager()
        op_id = UUID(int=_AUTHORIZED_UID)
        other_op = UUID(int=2000)
        cm.grant(
            tenant_id=UUID(int=1),
            human_operator_id=op_id,
            capability=Capability.DOCUMENTS,
            scope=ConsentScope.SESSION,
        )
        cm.grant(
            tenant_id=UUID(int=1),
            human_operator_id=other_op,
            capability=Capability.TERMINAL,
            scope=ConsentScope.SESSION,
        )
        wiring, _ = _make_wiring(consent_manager=cm)
        import json
        raw = wiring.list_consents(sender_uid=_AUTHORIZED_UID)
        items = json.loads(raw)
        assert len(items) == 1
        assert items[0]["capability"] == "documents"

    def test_no_operator_id_in_response(self) -> None:
        """No PII leakage: operator_id must not appear in list_consents output."""
        cm = ConsentManager()
        op_id = UUID(int=_AUTHORIZED_UID)
        cm.grant(
            tenant_id=UUID(int=1),
            human_operator_id=op_id,
            capability=Capability.DOCUMENTS,
            scope=ConsentScope.SESSION,
        )
        wiring, _ = _make_wiring(consent_manager=cm)
        import json
        raw = wiring.list_consents(sender_uid=_AUTHORIZED_UID)
        items = json.loads(raw)
        for item in items:
            assert "operator_id" not in item, "operator_id must not appear in list output"
            assert "human_operator_id" not in item


# ---------------------------------------------------------------------------
# Critical invariant: shared ConsentManager instance
# grant via D-Bus → assert_active() passes → revoke → ConsentDenied again
# ---------------------------------------------------------------------------


class TestSharedConsentManagerInvariant:
    """The ConsentManager injected into the wiring IS the one the broker uses.

    This test verifies the end-to-end gate: grant via D-Bus method, then call
    assert_active() on the SAME ConsentManager instance, and confirm it passes.
    After revoke, assert_active() must raise ConsentDenied.
    """

    def test_grant_makes_assert_active_pass(self) -> None:
        cm = ConsentManager()
        wiring, shared_cm = _make_wiring(consent_manager=cm)

        # Identical object — this is the broker's CM.
        assert shared_cm is cm

        wiring.grant_consent(
            capability="documents",
            scope="session",
            sender_uid=_AUTHORIZED_UID,
        )

        # The broker gate (assert_active) must now succeed on the same instance.
        op_id = UUID(int=_AUTHORIZED_UID)
        consent = cm.assert_active(
            human_operator_id=op_id,
            capability=Capability.DOCUMENTS,
        )
        assert consent.capability == Capability.DOCUMENTS

    def test_revoke_restores_consent_denied_gate(self) -> None:
        cm = ConsentManager()
        wiring, shared_cm = _make_wiring(consent_manager=cm)

        wiring.grant_consent(
            capability="documents",
            scope="session",
            sender_uid=_AUTHORIZED_UID,
        )
        op_id = UUID(int=_AUTHORIZED_UID)
        # Gate passes.
        cm.assert_active(human_operator_id=op_id, capability=Capability.DOCUMENTS)

        wiring.revoke_consent(
            capability="documents",
            sender_uid=_AUTHORIZED_UID,
        )
        # Gate must be restored — ConsentDenied raised.
        with pytest.raises(ConsentDenied):
            cm.assert_active(human_operator_id=op_id, capability=Capability.DOCUMENTS)
