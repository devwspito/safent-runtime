"""T-G2 🔒 — G2: denylist dura anti-autopirateo (CTRL-P2-2/3).

Verifica que cualquier operación (start/stop/restart/mask/disable) sobre un
servicio PROTEGIDO devuelve REJECTED_BY_POLICY, TERMINAL e inapelable por HITL,
evaluada ANTES de cualquier llamada a systemd.

Anti-aliasing (CTRL-P2-3): 'hermes-runtime', 'hermes-runtime.service',
'Hermes-Runtime' y otras variantes se rechazan igual.

Tests-first: FALLAN antes de CTRL-P2-2/3, PASAN después.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.unit

_SIGNING_KEY = os.urandom(32)
_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()


# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------


def _import_denylist():
    """Lazy import to keep test module importable even before implementation."""
    from hermes.capabilities.infrastructure.protected_service_denylist import (
        ProtectedServiceDenylist,
    )
    return ProtectedServiceDenylist


def _import_os_native_dispatcher():
    from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
    return OsNativeDispatcher


# ---------------------------------------------------------------------------
# CTRL-P2-2: denylist dura — protected services are rejected
# ---------------------------------------------------------------------------


class TestProtectedServiceDenylistRejects:
    """Los servicios del conjunto protegido son rechazados (CTRL-P2-2)."""

    @pytest.mark.parametrize("unit", [
        "hermes-runtime",
        "hermes-shell-server",
        "hermes-consent",
        "hermes-audit",
        "hermes-keygen",
    ])
    def test_protected_service_is_protected(self, unit: str) -> None:
        """Cada servicio del conjunto mínimo inviolable es marcado como protegido."""
        ProtectedServiceDenylist = _import_denylist()
        denylist = ProtectedServiceDenylist()
        assert denylist.is_protected(unit) is True, (
            f"'{unit}' debe estar en la denylist dura (CTRL-P2-2/G2). "
            "Conjunto mínimo: hermes-runtime, hermes-shell-server, "
            "hermes-consent, hermes-audit, hermes-keygen."
        )

    def test_unprotected_service_not_in_denylist(self) -> None:
        """Un servicio no protegido (p.ej. nginx) NO aparece en la denylist."""
        ProtectedServiceDenylist = _import_denylist()
        denylist = ProtectedServiceDenylist()
        assert denylist.is_protected("nginx") is False
        assert denylist.is_protected("postgresql") is False

    def test_protected_canonical_names_contains_all_minimum(self) -> None:
        """protected_canonical_names() contiene el conjunto mínimo completo."""
        ProtectedServiceDenylist = _import_denylist()
        denylist = ProtectedServiceDenylist()
        names = denylist.protected_canonical_names()
        for required in {
            "hermes-runtime",
            "hermes-shell-server",
            "hermes-consent",
            "hermes-audit",
            "hermes-keygen",
        }:
            assert required in names, (
                f"'{required}' debe aparecer en protected_canonical_names() (CTRL-P2-2)"
            )


# ---------------------------------------------------------------------------
# CTRL-P2-3: anti-aliasing — normalización canónica
# ---------------------------------------------------------------------------


class TestDenylistAntiAliasing:
    """La denylist resuelve por identidad canónica, no por cadena literal (CTRL-P2-3)."""

    @pytest.mark.parametrize("alias", [
        "hermes-runtime",
        "hermes-runtime.service",
        "Hermes-Runtime",
        "HERMES-RUNTIME",
        "hermes-runtime.service",
        "Hermes-Runtime.service",
    ])
    def test_hermes_runtime_aliasing(self, alias: str) -> None:
        """Todas las variantes de hermes-runtime son rechazadas (CTRL-P2-3)."""
        ProtectedServiceDenylist = _import_denylist()
        denylist = ProtectedServiceDenylist()
        assert denylist.is_protected(alias) is True, (
            f"Alias '{alias}' no fue rechazado por la denylist. "
            "CTRL-P2-3: la denylist debe resolver por identidad canónica."
        )

    @pytest.mark.parametrize("alias", [
        "hermes-consent",
        "hermes-consent.service",
        "Hermes-Consent",
        "HERMES-CONSENT.SERVICE",
        "hermes-consent.service",
        "Hermes-Consent.service",
    ])
    def test_hermes_consent_aliasing(self, alias: str) -> None:
        """Todas las variantes de hermes-consent son rechazadas (CTRL-P2-3)."""
        ProtectedServiceDenylist = _import_denylist()
        denylist = ProtectedServiceDenylist()
        assert denylist.is_protected(alias) is True, (
            f"Alias '{alias}' de hermes-consent no fue rechazado."
        )

    @pytest.mark.parametrize("alias", [
        "hermes-audit",
        "hermes-audit.service",
        "Hermes-Audit",
        "HERMES-AUDIT",
        "hermes-audit.service",
        "Hermes-Audit.service",
    ])
    def test_hermes_audit_aliasing(self, alias: str) -> None:
        ProtectedServiceDenylist = _import_denylist()
        denylist = ProtectedServiceDenylist()
        assert denylist.is_protected(alias) is True

    @pytest.mark.parametrize("alias", [
        "hermes-keygen",
        "hermes-keygen.service",
        "Hermes-Keygen",
        "HERMES-KEYGEN",
        "hermes-keygen.service",
        "Hermes-Keygen.service",
    ])
    def test_hermes_keygen_aliasing(self, alias: str) -> None:
        ProtectedServiceDenylist = _import_denylist()
        denylist = ProtectedServiceDenylist()
        assert denylist.is_protected(alias) is True

    @pytest.mark.parametrize("alias", [
        "hermes-shell-server",
        "hermes-shell-server.service",
        "Hermes-Shell-Server",
        "HERMES-SHELL-SERVER",
        "hermes-shell-server.service",
        "Hermes-Shell-Server.service",
    ])
    def test_hermes_shell_server_aliasing(self, alias: str) -> None:
        ProtectedServiceDenylist = _import_denylist()
        denylist = ProtectedServiceDenylist()
        assert denylist.is_protected(alias) is True


# ---------------------------------------------------------------------------
# CTRL-P2-2: OsNativeDispatcher rejects protected service ops
# ---------------------------------------------------------------------------


class TestOsNativeDispatcherRejectsProtectedServices:
    """El OsNativeDispatcher rechaza ops sobre servicios protegidos ANTES de systemd."""

    @pytest.mark.parametrize("operation", [
        "start_service",
        "stop_service",
        "restart_service",
    ])
    @pytest.mark.parametrize("unit", [
        "hermes-runtime",
        "hermes-consent",
        "hermes-audit",
    ])
    async def test_operation_on_protected_service_is_rejected(
        self, operation: str, unit: str
    ) -> None:
        """start/stop/restart sobre servicio protegido ⇒ resultado ok=False, policy (CTRL-P2-2)."""
        OsNativeDispatcher = _import_os_native_dispatcher()
        dispatcher = OsNativeDispatcher()

        result = await dispatcher.execute(
            skill_name=operation,
            args={"unit": unit, "reason": "test"},
        )

        assert result["ok"] is False, (
            f"{operation}({unit!r}) debe devolver ok=False — servicio protegido (CTRL-P2-2)"
        )
        assert "REJECTED_BY_POLICY" in str(result.get("reason", "")), (
            f"reason debe contener 'REJECTED_BY_POLICY', got: {result.get('reason')}"
        )

    @pytest.mark.parametrize("alias", [
        "hermes-runtime.service",
        "Hermes-Runtime",
        "HERMES-RUNTIME",
    ])
    async def test_aliased_protected_service_rejected(self, alias: str) -> None:
        """Alias del servicio protegido también rechazados (CTRL-P2-3)."""
        OsNativeDispatcher = _import_os_native_dispatcher()
        dispatcher = OsNativeDispatcher()

        result = await dispatcher.execute(
            skill_name="stop_service",
            args={"unit": alias, "reason": "test"},
        )

        assert result["ok"] is False, (
            f"Alias '{alias}' no fue rechazado por la denylist (CTRL-P2-3)"
        )
        assert "REJECTED_BY_POLICY" in str(result.get("reason", ""))

    async def test_unprotected_service_is_not_blocked(self) -> None:
        """Un servicio no protegido NO es rechazado por la denylist (solo systemd fallaría)."""
        OsNativeDispatcher = _import_os_native_dispatcher()
        # Inject a mock systemd to avoid real systemctl calls
        from unittest.mock import AsyncMock, patch

        dispatcher = OsNativeDispatcher()

        # For non-protected services the denylist should pass through
        # (systemd execution itself may fail in tests, but the denylist gate must NOT block it)
        # We patch systemd execution to avoid real calls
        with patch.object(dispatcher, "_run_systemctl", new=AsyncMock(return_value={"ok": True})):
            result = await dispatcher.execute(
                skill_name="start_service",
                args={"unit": "nginx", "reason": "test"},
            )

        # The denylist gate was NOT triggered — result comes from systemctl
        assert result["ok"] is True, (
            "nginx (no protegido) no debe ser rechazado por la denylist"
        )


# ---------------------------------------------------------------------------
# CTRL-P2-2: denylist is terminal — HITL token does NOT unblock protected ops
# ---------------------------------------------------------------------------


class TestDenylistIsTerminalAndInapelable:
    """REJECTED_BY_POLICY de denylist es terminal — HITL no puede desbloquear (NFR-002)."""

    async def test_denylist_rejection_is_not_overridable_by_hitl_token(self) -> None:
        """Incluso con token HITL válido, servicio protegido sigue rechazado (NFR-002)."""
        OsNativeDispatcher = _import_os_native_dispatcher()
        dispatcher = OsNativeDispatcher()

        # Pass a "valid" token — must still be rejected
        result = await dispatcher.execute(
            skill_name="stop_service",
            args={
                "unit": "hermes-audit",
                "reason": "test hitl bypass attempt",
                "hitl_approval_token": "any_token_here",
            },
        )

        assert result["ok"] is False
        assert "REJECTED_BY_POLICY" in str(result.get("reason", "")), (
            "La denylist es terminal e inapelable por HITL (NFR-002/CTRL-P2-2). "
            "Incluso con token, hermes-audit es rechazado."
        )


# ---------------------------------------------------------------------------
# CTRL-P2-2: Broker-level: os_native binding on protected service → rejected
# ---------------------------------------------------------------------------


class TestBrokerRejectsProtectedServiceViaOsNative:
    """El broker, con un binding os_native para stop_service en hermes-runtime,
    devuelve REJECTED_BY_POLICY sin ejecutar nada (G2 end-to-end).
    """

    async def test_broker_dispatch_stop_hermes_runtime_is_rejected(self) -> None:
        """Dispatch de stop_service(hermes-runtime) via broker ⇒ REJECTED_BY_POLICY (G2)."""
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.capabilities.application.capability_broker import CapabilityBroker
        from hermes.capabilities.application.intent_log import IntentLog
        from hermes.capabilities.domain.ports import CapabilityBinding, ConsentContext, RiskLevel
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
        from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from hermes.domain.proposal import ToolCallProposal

        OsNativeDispatcher = _import_os_native_dispatcher()

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
        os_disp = OsNativeDispatcher()
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
            parameters={"unit": "hermes-runtime", "reason": "autopiracy attempt"},
            justification="test",
        )
        ctx = ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID)

        # Even with valid HITL token the denylist blocks it
        outcome = await broker.dispatch(proposal, ctx, hitl_approval_token="any_token")

        from hermes.capabilities.domain.ports import ExecutionStatus
        assert outcome.status == ExecutionStatus.REJECTED_BY_POLICY, (
            "stop_service(hermes-runtime) debe ser REJECTED_BY_POLICY via broker (G2)"
        )


# ---------------------------------------------------------------------------
# Fake helpers needed by the broker-level test
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
