"""Tests TenantBindingService (FR-019, FR-020, FR-032)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.agents_os.application.tenant_binding_service import (
    ActiveBindingExistsError,
    BindingStateInvalid,
    TenantBindingService,
    TenantBindingState,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def svc() -> TenantBindingService:
    return TenantBindingService()


class TestBind:
    def test_first_bind_active(
        self, svc: TenantBindingService
    ) -> None:
        nid = uuid4()
        tid = uuid4()
        b = svc.bind(node_installation_id=nid, tenant_id=tid)
        assert b.state == TenantBindingState.ACTIVE
        assert b.tenant_id == tid
        assert svc.has_active_binding(node_installation_id=nid)

    def test_second_active_bind_blocked(
        self, svc: TenantBindingService
    ) -> None:
        nid = uuid4()
        svc.bind(node_installation_id=nid, tenant_id=uuid4())
        with pytest.raises(ActiveBindingExistsError):
            svc.bind(node_installation_id=nid, tenant_id=uuid4())

    def test_rebind_after_revoke_ok(
        self, svc: TenantBindingService
    ) -> None:
        nid = uuid4()
        svc.bind(node_installation_id=nid, tenant_id=uuid4())
        svc.revoke(node_installation_id=nid, cause="tenant_offboarded")
        b = svc.bind(node_installation_id=nid, tenant_id=uuid4())
        assert b.state == TenantBindingState.ACTIVE


class TestRevoke:
    def test_revoke_sets_state_and_cause(
        self, svc: TenantBindingService
    ) -> None:
        nid = uuid4()
        svc.bind(node_installation_id=nid, tenant_id=uuid4())
        b = svc.revoke(node_installation_id=nid, cause="x")
        assert b.state == TenantBindingState.REVOKED
        assert b.revocation_cause == "x"

    def test_revoke_idempotent(
        self, svc: TenantBindingService
    ) -> None:
        nid = uuid4()
        svc.bind(node_installation_id=nid, tenant_id=uuid4())
        svc.revoke(node_installation_id=nid, cause="x")
        b = svc.revoke(node_installation_id=nid, cause="y")
        assert b.state == TenantBindingState.REVOKED


class TestRebind:
    def test_rebind_flow(
        self, svc: TenantBindingService
    ) -> None:
        nid = uuid4()
        old_tid = uuid4()
        new_tid = uuid4()
        svc.bind(node_installation_id=nid, tenant_id=old_tid)
        svc.begin_rebind(node_installation_id=nid)
        b = svc.complete_rebind(
            node_installation_id=nid, new_tenant_id=new_tid
        )
        assert b.state == TenantBindingState.ACTIVE
        assert b.tenant_id == new_tid
        assert b.last_rebound_at is not None

    def test_complete_without_begin_blocked(
        self, svc: TenantBindingService
    ) -> None:
        nid = uuid4()
        svc.bind(node_installation_id=nid, tenant_id=uuid4())
        with pytest.raises(BindingStateInvalid):
            svc.complete_rebind(
                node_installation_id=nid, new_tenant_id=uuid4()
            )


class TestQuery:
    def test_get_returns_none_when_unknown(
        self, svc: TenantBindingService
    ) -> None:
        assert svc.get(node_installation_id=uuid4()) is None

    def test_has_active_binding_false_when_revoked(
        self, svc: TenantBindingService
    ) -> None:
        nid = uuid4()
        svc.bind(node_installation_id=nid, tenant_id=uuid4())
        svc.revoke(node_installation_id=nid, cause="x")
        assert not svc.has_active_binding(node_installation_id=nid)
