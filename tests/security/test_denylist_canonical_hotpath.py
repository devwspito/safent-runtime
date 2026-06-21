"""T-G2-CANONICAL 🔒 — CONDITION-2: canonical identity in the WRITE hot-path.

Verifies that both OsNativeDispatcher._dispatch_service_mutation AND
CapabilityBroker._check_denylist use is_protected_canonical (not the lexical
is_protected) so that a real systemd alias cannot bypass the denylist.

Tests-first: these verify the gate condition, not a stub.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit

_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()
_SIGNING_KEY = b"canonical-hotpath-test-key-XX!!!"


class TestDispatcherUsesCanonicalInHotPath:
    """OsNativeDispatcher calls is_protected_canonical (not is_protected) for WRITE ops."""

    async def test_dispatcher_calls_is_protected_canonical(self) -> None:
        """Service mutation hot-path calls is_protected_canonical, not just is_protected."""
        from unittest.mock import MagicMock, patch

        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from hermes.capabilities.infrastructure.protected_service_denylist import (
            ProtectedServiceDenylist,
        )

        denylist = ProtectedServiceDenylist()
        dispatcher = OsNativeDispatcher(denylist=denylist)

        canonical_calls: list[str] = []
        original = denylist.is_protected_canonical

        def recording_canonical(unit: str) -> bool:
            canonical_calls.append(unit)
            return original(unit)

        with patch.object(denylist, "is_protected_canonical", side_effect=recording_canonical):
            await dispatcher.execute(
                skill_name="stop_service",
                args={"unit": "nginx.service"},
            )

        assert len(canonical_calls) >= 1, (
            "OsNativeDispatcher must call is_protected_canonical in the service "
            "mutation hot-path (CONDITION-2). Got zero calls."
        )
        assert "nginx.service" in canonical_calls, (
            f"Expected 'nginx.service' in canonical_calls, got {canonical_calls}"
        )

    async def test_dispatcher_canonical_check_catches_alias_not_in_lexical(self) -> None:
        """Simulated systemd alias (unknown by lexical check) is caught by canonical check."""
        from unittest.mock import patch

        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from hermes.capabilities.infrastructure.protected_service_denylist import (
            ProtectedServiceDenylist,
        )

        denylist = ProtectedServiceDenylist()
        dispatcher = OsNativeDispatcher(denylist=denylist)

        # "hermes-rt" is not in lexical set but systemctl show would reveal it's hermes-runtime.
        # We simulate that by patching _systemctl_show_id_names on the denylist.
        fake_output = "Id=hermes-runtime.service\nNames=hermes-runtime.service hermes-rt.service\n"
        with patch.object(denylist, "_systemctl_show_id_names", return_value=fake_output):
            result = await dispatcher.execute(
                skill_name="stop_service",
                args={"unit": "hermes-rt"},
            )

        assert result["ok"] is False, (
            "Alias 'hermes-rt' resolved via systemctl show to hermes-runtime.service "
            "must be REJECTED_BY_POLICY (CONDITION-2)."
        )
        assert "REJECTED_BY_POLICY" in str(result.get("reason", "")), (
            f"Reason must contain REJECTED_BY_POLICY. Got: {result.get('reason')}"
        )

    async def test_dispatcher_canonical_fallback_when_systemctl_unavailable(self) -> None:
        """When systemctl show fails, lexical fallback still catches known protected services."""
        from unittest.mock import patch

        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from hermes.capabilities.infrastructure.protected_service_denylist import (
            ProtectedServiceDenylist,
        )

        denylist = ProtectedServiceDenylist()
        dispatcher = OsNativeDispatcher(denylist=denylist)

        with patch.object(
            denylist,
            "_systemctl_show_id_names",
            side_effect=Exception("systemd not available"),
        ):
            result = await dispatcher.execute(
                skill_name="stop_service",
                args={"unit": "hermes-audit.service"},
            )

        assert result["ok"] is False, (
            "Lexical fallback must catch 'hermes-audit.service' when systemctl show fails."
        )
        assert "REJECTED_BY_POLICY" in str(result.get("reason", ""))


class TestBrokerUsesCanonicalInHotPath:
    """CapabilityBroker._check_denylist calls is_protected_canonical (not is_protected)."""

    async def test_broker_denylist_check_uses_canonical(self) -> None:
        """Broker._check_denylist calls is_protected_canonical for os_native service ops."""
        from unittest.mock import AsyncMock, patch

        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.capabilities.application.capability_broker import CapabilityBroker
        from hermes.capabilities.application.intent_log import IntentLog
        from hermes.capabilities.domain.ports import (
            CapabilityBinding,
            ConsentContext,
            RiskLevel,
        )
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
        from hermes.domain.proposal import ToolCallProposal

        os_disp = OsNativeDispatcher()
        denylist = os_disp._denylist

        canonical_calls: list[str] = []
        original = denylist.is_protected_canonical

        def recording_canonical(unit: str) -> bool:
            canonical_calls.append(unit)
            return original(unit)

        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="stop_service",
            surface_kind=None,
            required_capability=None,
            risk=RiskLevel.HIGH,
            auto_executable=False,
            executor="os_native",
        ))
        gate = FakeApprovalGate(auto_approve=True)
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        audit_repo = _InMemoryAuditRepo()
        intent_log = IntentLog()
        surface_dispatcher = SurfaceAdapterDispatcher(adapters={})
        consent = _FakeConsentManager()

        broker = CapabilityBroker(
            registry=reg,
            consent_manager=consent,
            approval_gate=gate,
            dispatcher=surface_dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=intent_log,
            os_native_dispatcher=os_disp,
        )

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="stop_service",
            tenant_id=_TENANT_ID,
            entity_id="test",
            entity_type="test",
            parameters={"unit": "nginx.service"},
            justification="canonical hotpath test",
        )
        ctx = ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID)

        with patch.object(denylist, "is_protected_canonical", side_effect=recording_canonical):
            await broker.dispatch(proposal, ctx, hitl_approval_token="any-token")

        assert len(canonical_calls) >= 1, (
            "Broker._check_denylist must call is_protected_canonical (not is_protected) "
            "for os_native service mutation ops (CONDITION-2)."
        )

    async def test_broker_canonical_detects_systemd_alias_on_protected_service(self) -> None:
        """Broker rejects a service that resolves to hermes-runtime via systemctl show."""
        from unittest.mock import patch

        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.capabilities.application.capability_broker import CapabilityBroker
        from hermes.capabilities.application.intent_log import IntentLog
        from hermes.capabilities.domain.ports import (
            CapabilityBinding,
            ConsentContext,
            ExecutionStatus,
            RiskLevel,
        )
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
        from hermes.domain.proposal import ToolCallProposal

        os_disp = OsNativeDispatcher()
        denylist = os_disp._denylist

        fake_show = "Id=hermes-runtime.service\nNames=hermes-runtime.service hermes-rt.service\n"

        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="stop_service",
            surface_kind=None,
            required_capability=None,
            risk=RiskLevel.HIGH,
            auto_executable=False,
            executor="os_native",
        ))
        gate = FakeApprovalGate(auto_approve=True)
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        audit_repo = _InMemoryAuditRepo()
        intent_log = IntentLog()
        surface_dispatcher = SurfaceAdapterDispatcher(adapters={})
        consent = _FakeConsentManager()

        broker = CapabilityBroker(
            registry=reg,
            consent_manager=consent,
            approval_gate=gate,
            dispatcher=surface_dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=intent_log,
            os_native_dispatcher=os_disp,
        )

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="stop_service",
            tenant_id=_TENANT_ID,
            entity_id="test",
            entity_type="test",
            parameters={"unit": "hermes-rt"},
            justification="canonical alias bypass attempt",
        )
        ctx = ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID)

        with patch.object(denylist, "_systemctl_show_id_names", return_value=fake_show):
            outcome = await broker.dispatch(proposal, ctx, hitl_approval_token="any-token")

        assert outcome.status == ExecutionStatus.REJECTED_BY_POLICY, (
            "Broker must reject 'hermes-rt' resolved via systemctl show to hermes-runtime "
            f"(CONDITION-2 canonical identity). Got {outcome.status}: {outcome.error}"
        )


# ---------------------------------------------------------------------------
# Fake helpers
# ---------------------------------------------------------------------------


class _InMemoryAuditRepo:
    def __init__(self) -> None:
        self.entries: list = []

    async def append(self, entry) -> None:
        self.entries.append(entry)

    async def head_hash_hex(self) -> str | None:
        return None

    async def load_chain(self, *, tenant_id=None) -> list:
        return list(self.entries)


class _FakeConsentManager:
    def assert_active(self, *, human_operator_id, capability) -> object:
        return object()

    def use(self, *, human_operator_id, capability) -> object:
        return object()
