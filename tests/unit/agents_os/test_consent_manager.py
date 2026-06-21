"""Tests ConsentManager (FR-013 capability-based macOS-like)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.agents_os.application.consent_manager import (
    Capability,
    ConsentDenied,
    ConsentManager,
    ConsentScope,
)

pytestmark = pytest.mark.unit


class TestGrantAndRevoke:
    def test_grant_makes_capability_active(self) -> None:
        mgr = ConsentManager()
        op = uuid4()
        ten = uuid4()
        mgr.grant(
            tenant_id=ten,
            human_operator_id=op,
            capability=Capability.DOCUMENTS,
            scope=ConsentScope.PERSISTENT,
        )
        consent = mgr.assert_active(
            human_operator_id=op, capability=Capability.DOCUMENTS
        )
        assert consent.capability == Capability.DOCUMENTS
        assert consent.scope == ConsentScope.PERSISTENT

    def test_revoke_removes_active(self) -> None:
        mgr = ConsentManager()
        op = uuid4()
        ten = uuid4()
        mgr.grant(
            tenant_id=ten,
            human_operator_id=op,
            capability=Capability.TERMINAL,
            scope=ConsentScope.PERSISTENT,
        )
        revoked = mgr.revoke(
            human_operator_id=op, capability=Capability.TERMINAL
        )
        assert revoked is not None
        assert revoked.revoked_at is not None
        with pytest.raises(ConsentDenied):
            mgr.assert_active(
                human_operator_id=op, capability=Capability.TERMINAL
            )

    def test_revoke_unknown_returns_none(self) -> None:
        mgr = ConsentManager()
        result = mgr.revoke(
            human_operator_id=uuid4(), capability=Capability.CAMERA
        )
        assert result is None


class TestFailClosed:
    def test_no_consent_raises_denied(self) -> None:
        mgr = ConsentManager()
        with pytest.raises(ConsentDenied, match="No hay consent activo"):
            mgr.assert_active(
                human_operator_id=uuid4(),
                capability=Capability.FILESYSTEM_FULL,
            )

    def test_other_operator_does_not_share_consent(self) -> None:
        mgr = ConsentManager()
        op_a = uuid4()
        op_b = uuid4()
        ten = uuid4()
        mgr.grant(
            tenant_id=ten,
            human_operator_id=op_a,
            capability=Capability.MICROPHONE,
            scope=ConsentScope.PERSISTENT,
        )
        # B no comparte el consent de A
        with pytest.raises(ConsentDenied):
            mgr.assert_active(
                human_operator_id=op_b, capability=Capability.MICROPHONE
            )


class TestScopes:
    def test_once_scope_invalidates_after_first_use(self) -> None:
        mgr = ConsentManager()
        op = uuid4()
        ten = uuid4()
        mgr.grant(
            tenant_id=ten,
            human_operator_id=op,
            capability=Capability.DOWNLOADS,
            scope=ConsentScope.ONCE,
        )
        # Uso 1: OK
        mgr.use(human_operator_id=op, capability=Capability.DOWNLOADS)
        # Uso 2: bloqueado porque ONCE
        with pytest.raises(ConsentDenied):
            mgr.use(human_operator_id=op, capability=Capability.DOWNLOADS)

    def test_persistent_scope_allows_multiple_uses(self) -> None:
        mgr = ConsentManager()
        op = uuid4()
        ten = uuid4()
        mgr.grant(
            tenant_id=ten,
            human_operator_id=op,
            capability=Capability.NETWORK_LOCAL,
            scope=ConsentScope.PERSISTENT,
        )
        for _ in range(5):
            mgr.use(human_operator_id=op, capability=Capability.NETWORK_LOCAL)
        consent = mgr.assert_active(
            human_operator_id=op, capability=Capability.NETWORK_LOCAL
        )
        assert consent.usage_count == 5


class TestListAndAudit:
    def test_list_active_returns_only_active(self) -> None:
        mgr = ConsentManager()
        op = uuid4()
        ten = uuid4()
        for cap in (Capability.DOCUMENTS, Capability.CAMERA):
            mgr.grant(
                tenant_id=ten,
                human_operator_id=op,
                capability=cap,
                scope=ConsentScope.PERSISTENT,
            )
        mgr.revoke(human_operator_id=op, capability=Capability.CAMERA)
        active = mgr.list_active(human_operator_id=op)
        caps = {c.capability for c in active}
        assert caps == {Capability.DOCUMENTS}

    def test_audit_log_records_grants_and_revokes(self) -> None:
        mgr = ConsentManager()
        op = uuid4()
        ten = uuid4()
        mgr.grant(
            tenant_id=ten,
            human_operator_id=op,
            capability=Capability.SYSTEM_SETTINGS,
            scope=ConsentScope.SESSION,
        )
        mgr.revoke(
            human_operator_id=op, capability=Capability.SYSTEM_SETTINGS
        )
        assert len(mgr.granted_log) == 1
        assert len(mgr.revoked_log) == 1
