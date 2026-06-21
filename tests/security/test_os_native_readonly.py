"""T-US1 🔒 — OS-native READ_ONLY skills: patrón broker-first, consent default-deny, audit.

US1 / FR-001..004 / SC-005 / SC-006.

Verifica:
- list_services / get_service_status / get_system_info / list_devices / list_audio_devices
  pasan POR el broker (consent gate + audit entry producido).
- Sin consent concedido: REJECTED_BY_CONSENT, fail-closed, 0 ejecuciones.
- Con consent: EXECUTED, audit entry PROPOSAL_EXECUTED, 0 HITL requerido.
- El OsNativeDispatcher._dispatch_read_only implementa REALMENTE cada skill
  (no devuelve _stub=True — CONDITION verifica ausencia del stub).
- Inyección de comando/lectura fake: mockeable sin systemd real.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner, AuditKind
from hermes.agents_os.application.consent_manager import (
    Capability,
    ConsentManager,
    ConsentScope,
)
from hermes.capabilities.application.capability_broker import CapabilityBroker
from hermes.capabilities.application.intent_log import IntentLog
from hermes.capabilities.domain.ports import (
    CapabilityBinding,
    ConsentContext,
    ExecutionStatus,
    RiskLevel,
)
from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry

pytestmark = pytest.mark.unit

_SIGNING_KEY = b"test-key-32-bytes-fixed-value!XY"
_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()

# READ_ONLY skills defined by the 007 contract.
READ_ONLY_SKILLS: list[str] = [
    "list_services",
    "get_service_status",
    "get_system_info",
    "list_devices",
    "list_audio_devices",
]

# capability required for each skill (per contract os_native_skills.py)
_SKILL_TO_CAPABILITY: dict[str, str] = {
    "list_services": "system_services",
    "get_service_status": "system_services",
    "get_system_info": "system_info",
    "list_devices": "udev_devices",
    "list_audio_devices": "audio_devices",
}


# ---------------------------------------------------------------------------
# Fake infrastructure
# ---------------------------------------------------------------------------


class _FakeReadOnlyDispatcher:
    """Simulates OsNativeDispatcher for READ_ONLY skills (mockeable, no systemd)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, *, skill_name: str, args: dict) -> dict:
        self.calls.append((skill_name, args))
        # Simulate real (non-stub) responses per skill.
        if skill_name == "list_services":
            return {"ok": True, "services": [{"unit": "nginx.service", "active_state": "active"}]}
        if skill_name == "get_service_status":
            return {
                "ok": True,
                "unit": args.get("unit", "unknown"),
                "active_state": "active",
                "sub_state": "running",
                "load_state": "loaded",
            }
        if skill_name == "get_system_info":
            return {
                "ok": True,
                "hostname": "test-host",
                "kernel": "6.0.0",
                "uptime_s": 12345,
                "load": [0.1, 0.2, 0.3],
                "mem": {"total_kb": 16000000, "available_kb": 8000000},
            }
        if skill_name == "list_devices":
            return {"ok": True, "devices": [{"name": "sda", "subsystem": "block", "sys_path": "/sys/block/sda"}]}
        if skill_name == "list_audio_devices":
            return {"ok": True, "sources": [], "sinks": [{"name": "Built-in Audio"}]}
        return {"ok": False, "reason": f"skill desconocida: {skill_name!r}"}

    def supports(self, skill_name: str) -> bool:
        return skill_name in frozenset(READ_ONLY_SKILLS)


class _InMemoryAuditRepo:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def append(self, entry: Any) -> None:
        self.entries.append(entry)

    async def head_hash_hex(self) -> str | None:
        return None

    async def load_chain(self, *, tenant_id: UUID | None = None) -> list[Any]:
        return list(self.entries)


def _make_consent_manager_with_grant(capability: str) -> tuple[ConsentManager, UUID]:
    """Returns a ConsentManager with one PERSISTENT grant for operator."""
    operator_id = _OPERATOR_ID
    mgr = ConsentManager()
    mgr.grant(
        tenant_id=_TENANT_ID,
        human_operator_id=operator_id,
        capability=Capability(capability),
        scope=ConsentScope.PERSISTENT,
    )
    return mgr, operator_id


def _make_broker(
    *,
    fake_dispatcher: _FakeReadOnlyDispatcher,
    consent_manager: ConsentManager,
    skill_name: str,
    capability: str | None,
    risk: RiskLevel = RiskLevel.LOW,
    auto_executable: bool = True,
) -> tuple[CapabilityBroker, _InMemoryAuditRepo]:
    from hermes.capabilities.infrastructure.surface_adapter_dispatcher import SurfaceAdapterDispatcher

    reg = FakeCapabilityRegistry()
    reg.register(CapabilityBinding(
        tool_name=skill_name,
        surface_kind=None,
        required_capability=capability,
        risk=risk,
        auto_executable=auto_executable,
        executor="os_native",
    ))
    gate = FakeApprovalGate()
    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    audit_repo = _InMemoryAuditRepo()
    intent_log = IntentLog()
    surface_dispatcher = SurfaceAdapterDispatcher(adapters={})

    broker = CapabilityBroker(
        registry=reg,
        consent_manager=consent_manager,
        approval_gate=gate,
        dispatcher=surface_dispatcher,
        signer=signer,
        audit_repo=audit_repo,
        intent_log=intent_log,
        os_native_dispatcher=fake_dispatcher,
    )
    return broker, audit_repo


def _proposal(tool_name: str, params: dict | None = None):
    from hermes.domain.proposal import ToolCallProposal
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT_ID,
        entity_id="test",
        entity_type="test",
        parameters=params or {},
        justification="US1 test",
    )


def _ctx(operator_id: UUID | None = None) -> ConsentContext:
    return ConsentContext(
        tenant_id=_TENANT_ID,
        operator_id=operator_id or _OPERATOR_ID,
    )


# ---------------------------------------------------------------------------
# US1-A: READ_ONLY skills execute without HITL when consent is granted
# ---------------------------------------------------------------------------


class TestReadOnlySkillsExecuteWithConsent:
    """With consent granted, READ_ONLY skills execute directly (no HITL) and produce audit."""

    @pytest.mark.parametrize("skill_name", READ_ONLY_SKILLS)
    async def test_skill_executes_and_produces_audit(self, skill_name: str) -> None:
        """READ_ONLY skill with consent → EXECUTED + PROPOSAL_EXECUTED audit entry (US1 AC1)."""
        capability = _SKILL_TO_CAPABILITY[skill_name]
        mgr, operator_id = _make_consent_manager_with_grant(capability)
        fake_disp = _FakeReadOnlyDispatcher()
        broker, audit_repo = _make_broker(
            fake_dispatcher=fake_disp,
            consent_manager=mgr,
            skill_name=skill_name,
            capability=capability,
        )

        params = {"unit": "nginx.service"} if skill_name == "get_service_status" else {}
        outcome = await broker.dispatch(_proposal(skill_name, params), _ctx(operator_id))

        assert outcome.status == ExecutionStatus.EXECUTED, (
            f"{skill_name}: expected EXECUTED with consent, got {outcome.status}: {outcome.error}"
        )
        executed_entries = [
            e for e in audit_repo.entries if e.audit_kind == AuditKind.PROPOSAL_EXECUTED
        ]
        assert len(executed_entries) == 1, (
            f"{skill_name}: debe producir exactamente 1 AuditEntry PROPOSAL_EXECUTED "
            f"(US1 AC1/FR-004). Got {len(executed_entries)}."
        )

    @pytest.mark.parametrize("skill_name", READ_ONLY_SKILLS)
    async def test_skill_does_not_require_hitl(self, skill_name: str) -> None:
        """READ_ONLY skill with consent → no HITL required (US1 AC1, FR-004)."""
        capability = _SKILL_TO_CAPABILITY[skill_name]
        mgr, operator_id = _make_consent_manager_with_grant(capability)
        fake_disp = _FakeReadOnlyDispatcher()
        broker, _ = _make_broker(
            fake_dispatcher=fake_disp,
            consent_manager=mgr,
            skill_name=skill_name,
            capability=capability,
        )

        outcome = await broker.dispatch(
            _proposal(skill_name),
            _ctx(operator_id),
            hitl_approval_token=None,  # no token — should still execute
        )

        assert outcome.status == ExecutionStatus.EXECUTED, (
            f"{skill_name}: READ_ONLY should execute without HITL token (FR-004). "
            f"Got {outcome.status}: {outcome.error}"
        )
        assert len(fake_disp.calls) == 1, (
            f"{skill_name}: executor should be called once, got {len(fake_disp.calls)}"
        )

    @pytest.mark.parametrize("skill_name", READ_ONLY_SKILLS)
    async def test_skill_routes_through_os_native_dispatcher(self, skill_name: str) -> None:
        """READ_ONLY skill passes through OsNativeDispatcher, not surface_adapter (US1 AC4)."""
        capability = _SKILL_TO_CAPABILITY[skill_name]
        mgr, operator_id = _make_consent_manager_with_grant(capability)
        fake_disp = _FakeReadOnlyDispatcher()
        broker, _ = _make_broker(
            fake_dispatcher=fake_disp,
            consent_manager=mgr,
            skill_name=skill_name,
            capability=capability,
        )

        await broker.dispatch(_proposal(skill_name), _ctx(operator_id))

        # The fake dispatcher was invoked — proof the os_native branch was taken.
        assert len(fake_disp.calls) == 1, (
            f"{skill_name}: OsNativeDispatcher NOT called. "
            "Skill must route via os_native branch (US1 AC4/FR-002)."
        )
        assert fake_disp.calls[0][0] == skill_name


# ---------------------------------------------------------------------------
# US1-B: Consent denied by default → fail-closed
# ---------------------------------------------------------------------------


class TestReadOnlySkillsDeniedWithoutConsent:
    """Without consent (default state) → REJECTED_BY_CONSENT, 0 executions (US1 AC3/SC-006)."""

    @pytest.mark.parametrize("skill_name", READ_ONLY_SKILLS)
    async def test_skill_denied_without_consent(self, skill_name: str) -> None:
        """No consent → REJECTED_BY_CONSENT, executor not called (US1 AC3)."""
        capability = _SKILL_TO_CAPABILITY[skill_name]
        mgr = ConsentManager()  # no grants — default-deny
        fake_disp = _FakeReadOnlyDispatcher()
        broker, audit_repo = _make_broker(
            fake_dispatcher=fake_disp,
            consent_manager=mgr,
            skill_name=skill_name,
            capability=capability,
        )

        outcome = await broker.dispatch(_proposal(skill_name), _ctx())

        assert outcome.status == ExecutionStatus.REJECTED_BY_CONSENT, (
            f"{skill_name}: must be REJECTED_BY_CONSENT without consent (US1 AC3/SC-006). "
            f"Got {outcome.status}: {outcome.error}"
        )
        assert len(fake_disp.calls) == 0, (
            f"{skill_name}: executor must NOT be called when consent is denied (fail-closed)."
        )

    @pytest.mark.parametrize("skill_name", READ_ONLY_SKILLS)
    async def test_denial_produces_audit_trace(self, skill_name: str) -> None:
        """Consent denial leaves an audit trace (US1 AC3 / FR-003)."""
        capability = _SKILL_TO_CAPABILITY[skill_name]
        mgr = ConsentManager()
        fake_disp = _FakeReadOnlyDispatcher()
        broker, audit_repo = _make_broker(
            fake_dispatcher=fake_disp,
            consent_manager=mgr,
            skill_name=skill_name,
            capability=capability,
        )

        await broker.dispatch(_proposal(skill_name), _ctx())

        rejected_entries = [
            e for e in audit_repo.entries if e.audit_kind == AuditKind.PROPOSAL_REJECTED
        ]
        assert len(rejected_entries) >= 1, (
            f"{skill_name}: denial must leave an audit trace (FR-003)."
        )


# ---------------------------------------------------------------------------
# US1-C: OsNativeDispatcher._dispatch_read_only is NOT a stub
# ---------------------------------------------------------------------------


class TestDispatcherReadOnlyIsNotStub:
    """OsNativeDispatcher._dispatch_read_only must implement real logic (no _stub=True).

    This test verifies each READ_ONLY skill returns a properly structured response
    that does NOT contain the '_stub' flag from the old placeholder implementation.
    """

    @pytest.mark.parametrize("skill_name", READ_ONLY_SKILLS)
    async def test_real_dispatcher_does_not_return_stub(self, skill_name: str) -> None:
        """OsNativeDispatcher._dispatch_read_only must NOT return _stub=True."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from unittest.mock import AsyncMock, patch

        dispatcher = OsNativeDispatcher()

        # Inject fake subprocess so no real systemd calls happen.
        fake_proc_result = {
            "ok": True,
            "stdout": "[]",
            "stderr": "",
            "returncode": 0,
        }

        # For proc/sys based skills, patch the internal reading methods.
        with patch.object(
            dispatcher, "_run_systemctl", new=AsyncMock(return_value=fake_proc_result)
        ), patch.object(
            dispatcher, "_read_proc_files", return_value={
                "hostname": "test-host",
                "kernel": "6.0.0",
                "uptime_s": 0,
                "load": [0.0, 0.0, 0.0],
                "mem": {},
            }, create=True
        ), patch.object(
            dispatcher, "_enumerate_sysfs_devices", return_value=[], create=True
        ), patch.object(
            dispatcher, "_query_pipewire_devices", return_value={"sources": [], "sinks": []}, create=True
        ):
            result = await dispatcher._dispatch_read_only(skill_name, {})

        assert "_stub" not in result or result.get("_stub") is not True, (
            f"OsNativeDispatcher._dispatch_read_only('{skill_name}') returned a stub result. "
            "This must be replaced with real implementation (feature 007 Carril A / US1)."
        )

    async def test_list_services_returns_services_key(self) -> None:
        """list_services result has 'services' key (contract shape)."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from unittest.mock import AsyncMock, patch

        dispatcher = OsNativeDispatcher()
        # Patch subprocess to return empty JSON list
        fake_result = {"ok": True, "stdout": "[]", "stderr": "", "returncode": 0}
        with patch.object(dispatcher, "_run_systemctl", new=AsyncMock(return_value=fake_result)):
            result = await dispatcher._dispatch_read_only("list_services", {})

        assert "services" in result, (
            f"list_services must return dict with 'services' key. Got: {result}"
        )
        assert result.get("ok") is True

    async def test_get_service_status_returns_active_state(self) -> None:
        """get_service_status result has 'active_state' key."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from unittest.mock import AsyncMock, patch

        dispatcher = OsNativeDispatcher()
        fake_result = {
            "ok": True,
            "stdout": "ActiveState=active\nSubState=running\nLoadState=loaded\n",
            "stderr": "",
            "returncode": 0,
        }
        with patch.object(dispatcher, "_run_systemctl", new=AsyncMock(return_value=fake_result)):
            result = await dispatcher._dispatch_read_only("get_service_status", {"unit": "nginx.service"})

        assert result.get("ok") is True
        assert "active_state" in result, (
            f"get_service_status must return 'active_state'. Got: {result}"
        )

    async def test_get_system_info_returns_proc_fields(self) -> None:
        """get_system_info returns hostname, kernel, uptime_s, load, mem."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from unittest.mock import patch

        dispatcher = OsNativeDispatcher()
        with patch.object(
            dispatcher, "_read_proc_files", return_value={
                "hostname": "mybox",
                "kernel": "6.1.0",
                "uptime_s": 100,
                "load": [0.5, 0.6, 0.7],
                "mem": {"total_kb": 8000000, "available_kb": 4000000},
            }, create=True
        ):
            result = await dispatcher._dispatch_read_only("get_system_info", {})

        assert result.get("ok") is True
        for key in ("hostname", "kernel", "uptime_s", "load", "mem"):
            assert key in result, f"get_system_info missing '{key}' key. Got: {result}"

    async def test_list_devices_returns_devices_key(self) -> None:
        """list_devices result has 'devices' key."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from unittest.mock import patch

        dispatcher = OsNativeDispatcher()
        with patch.object(
            dispatcher, "_enumerate_sysfs_devices", return_value=[
                {"name": "sda", "subsystem": "block", "sys_path": "/sys/block/sda"}
            ], create=True
        ):
            result = await dispatcher._dispatch_read_only("list_devices", {})

        assert result.get("ok") is True
        assert "devices" in result, f"list_devices must return 'devices'. Got: {result}"

    async def test_list_audio_devices_returns_sources_sinks(self) -> None:
        """list_audio_devices result has 'sources' and 'sinks' keys."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from unittest.mock import patch

        dispatcher = OsNativeDispatcher()
        with patch.object(
            dispatcher, "_query_pipewire_devices", return_value={"sources": [], "sinks": []}, create=True
        ):
            result = await dispatcher._dispatch_read_only("list_audio_devices", {})

        assert result.get("ok") is True
        assert "sources" in result, f"list_audio_devices must return 'sources'. Got: {result}"
        assert "sinks" in result, f"list_audio_devices must return 'sinks'. Got: {result}"


# ---------------------------------------------------------------------------
# US1-D: Skill registry contains READ_ONLY skills with correct bindings
# ---------------------------------------------------------------------------


class TestRegistryContainsReadOnlySkills:
    """CapabilityRegistry has bindings for all US1 READ_ONLY skills."""

    @pytest.mark.parametrize("skill_name", READ_ONLY_SKILLS)
    def test_skill_registered_as_os_native(self, skill_name: str) -> None:
        from hermes.capabilities.application.capability_registry import CapabilityRegistry

        registry = CapabilityRegistry()
        binding = registry.resolve(skill_name)

        assert binding is not None, (
            f"'{skill_name}' not found in CapabilityRegistry. "
            "Must be registered with executor='os_native' (FR-005/US1)."
        )
        assert binding.executor == "os_native", (
            f"'{skill_name}' binding.executor must be 'os_native', got {binding.executor!r}"
        )

    @pytest.mark.parametrize("skill_name", READ_ONLY_SKILLS)
    def test_skill_has_correct_risk(self, skill_name: str) -> None:
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import RiskLevel

        registry = CapabilityRegistry()
        binding = registry.resolve(skill_name)

        assert binding is not None
        assert binding.risk == RiskLevel.LOW, (
            f"'{skill_name}' must have risk=LOW (READ_ONLY). Got {binding.risk}"
        )
        assert binding.auto_executable is True, (
            f"'{skill_name}' must be auto_executable=True (no HITL for READ_ONLY). "
            f"Got {binding.auto_executable}"
        )

    @pytest.mark.parametrize("skill_name,expected_cap", [
        ("list_services", "system_services"),
        ("get_service_status", "system_services"),
        ("get_system_info", "system_info"),
        ("list_devices", "udev_devices"),
        ("list_audio_devices", "audio_devices"),
    ])
    def test_skill_has_correct_capability(self, skill_name: str, expected_cap: str) -> None:
        from hermes.capabilities.application.capability_registry import CapabilityRegistry

        registry = CapabilityRegistry()
        binding = registry.resolve(skill_name)

        assert binding is not None
        assert binding.required_capability == expected_cap, (
            f"'{skill_name}' must require capability '{expected_cap}'. "
            f"Got {binding.required_capability!r}"
        )
