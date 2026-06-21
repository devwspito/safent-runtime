"""T-US4 🔒 — OS-native WRITE/HIGH skills: HITL obligatorio + denylist inviolable.

US4 / FR-006..009 / FR-011 / SC-002 / SC-005.

Verifica:
- start/stop/restart_service exigen HITL token válido (sin token → PENDING_APPROVAL).
- hermes-* (con cualquier alias) → REJECTED_BY_POLICY terminal, inapelable por HITL.
- schedule_task / unschedule_task / list_scheduled_tasks registrados (executor=os_native).
- CONDITION-2: denylist resuelve aliases vía systemctl show -p Id,Names con fallback léxico.
- tool_specs._execute_via_legacy_executor: sin dispatcher → REJECTED, nunca ejecuta raw executor.
- Pausa global (kill-switch) aplica igual a WRITE skills (FR-011 / SC-005).
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

_SIGNING_KEY = b"write-hitl-test-key-32bytes-XX!!"
_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()

SERVICE_MUTATION_SKILLS = ["start_service", "stop_service", "restart_service"]
SCHEDULER_WRITE_SKILLS = ["schedule_task", "unschedule_task"]
SCHEDULER_READ_SKILLS = ["list_scheduled_tasks"]

_PROTECTED_UNITS = [
    "hermes-runtime",
    "hermes-runtime.service",
    "Hermes-Runtime",
    "HERMES-RUNTIME",
    "hermes-shell-server",
    "hermes-shell-server.service",
    "hermes-consent",
    "hermes-consent.service",
    "Hermes-Consent",
    "hermes-audit",
    "hermes-audit.service",
    "Hermes-Audit",
    "hermes-keygen",
    "hermes-keygen.service",
]


# ---------------------------------------------------------------------------
# Fake infrastructure
# ---------------------------------------------------------------------------


class _RecordingDispatcher:
    """Dispatcher that records calls but doesn't actually execute anything."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._results: dict[str, dict] = {}

    def set_result(self, skill_name: str, result: dict) -> None:
        self._results[skill_name] = result

    async def execute(self, *, skill_name: str, args: dict) -> dict:
        self.calls.append((skill_name, args))
        return self._results.get(skill_name, {"ok": True})

    def supports(self, skill_name: str) -> bool:
        return True


class _InMemoryAuditRepo:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def append(self, entry: Any) -> None:
        self.entries.append(entry)

    async def head_hash_hex(self) -> str | None:
        return None

    async def load_chain(self, *, tenant_id: UUID | None = None) -> list[Any]:
        return list(self.entries)


def _make_consent_manager_with_system_services() -> ConsentManager:
    mgr = ConsentManager()
    mgr.grant(
        tenant_id=_TENANT_ID,
        human_operator_id=_OPERATOR_ID,
        capability=Capability.SYSTEM_SERVICES,
        scope=ConsentScope.PERSISTENT,
    )
    return mgr


def _make_broker(
    *,
    dispatcher: _RecordingDispatcher,
    consent_manager: ConsentManager,
    skill_name: str,
    capability: str,
    risk: RiskLevel,
    auto_executable: bool,
    fake_hitl_approved: bool = False,
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
    gate = FakeApprovalGate(auto_approve=fake_hitl_approved)
    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    audit_repo = _InMemoryAuditRepo()
    intent_log = IntentLog()
    surface_dispatcher = SurfaceAdapterDispatcher(adapters={})

    # Use real OsNativeDispatcher (with real denylist)
    from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
    real_dispatcher = OsNativeDispatcher()

    broker = CapabilityBroker(
        registry=reg,
        consent_manager=consent_manager,
        approval_gate=gate,
        dispatcher=surface_dispatcher,
        signer=signer,
        audit_repo=audit_repo,
        intent_log=intent_log,
        os_native_dispatcher=real_dispatcher,
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
        justification="US4 test",
    )


def _ctx(operator_id: UUID | None = None) -> ConsentContext:
    return ConsentContext(
        tenant_id=_TENANT_ID,
        operator_id=operator_id or _OPERATOR_ID,
    )


# ---------------------------------------------------------------------------
# US4-A: start/stop/restart require HITL token
# ---------------------------------------------------------------------------


class TestServiceMutationRequiresHitl:
    """start/stop/restart_service without HITL token → PENDING_APPROVAL (US4 AC1/FR-007)."""

    @pytest.mark.parametrize("skill_name", SERVICE_MUTATION_SKILLS)
    async def test_without_hitl_token_is_pending(self, skill_name: str) -> None:
        """No HITL token → PENDING_APPROVAL (FR-007/SC-005)."""
        mgr = _make_consent_manager_with_system_services()
        broker, _ = _make_broker(
            dispatcher=_RecordingDispatcher(),
            consent_manager=mgr,
            skill_name=skill_name,
            capability="system_services",
            risk=RiskLevel.HIGH,
            auto_executable=False,
        )

        outcome = await broker.dispatch(
            _proposal(skill_name, {"unit": "nginx.service", "reason": "test"}),
            _ctx(),
            hitl_approval_token=None,
        )

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL, (
            f"{skill_name}: without HITL token must be PENDING_APPROVAL (FR-007). "
            f"Got {outcome.status}: {outcome.error}"
        )


# ---------------------------------------------------------------------------
# US4-B: Protected services → REJECTED_BY_POLICY, terminal, inapelable
# ---------------------------------------------------------------------------


class TestProtectedServicesRejectedByDenylist:
    """Protected hermes-* services rejected by denylist even with HITL token (US4 AC2/FR-008)."""

    @pytest.mark.parametrize("unit", _PROTECTED_UNITS)
    @pytest.mark.parametrize("skill_name", SERVICE_MUTATION_SKILLS)
    async def test_protected_unit_rejected_by_policy(
        self, skill_name: str, unit: str
    ) -> None:
        """Any variant of a protected service is REJECTED_BY_POLICY (terminal, inapelable)."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import SurfaceAdapterDispatcher

        mgr = _make_consent_manager_with_system_services()
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name=skill_name,
            surface_kind=None,
            required_capability="system_services",
            risk=RiskLevel.HIGH,
            auto_executable=False,
            executor="os_native",
        ))
        # Use auto_approve=True to ensure HITL is NOT the gating factor.
        gate = FakeApprovalGate(auto_approve=True)
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        audit_repo = _InMemoryAuditRepo()
        intent_log = IntentLog()
        surface_dispatcher = SurfaceAdapterDispatcher(adapters={})
        os_disp = OsNativeDispatcher()

        broker = CapabilityBroker(
            registry=reg,
            consent_manager=mgr,
            approval_gate=gate,
            dispatcher=surface_dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=intent_log,
            os_native_dispatcher=os_disp,
        )

        outcome = await broker.dispatch(
            _proposal(skill_name, {"unit": unit, "reason": "autopiracy attempt"}),
            _ctx(),
            hitl_approval_token="any-valid-token",
        )

        assert outcome.status == ExecutionStatus.REJECTED_BY_POLICY, (
            f"{skill_name}(unit={unit!r}): protected service must be REJECTED_BY_POLICY "
            f"even with HITL token (FR-008/NFR-002). Got {outcome.status}: {outcome.error}"
        )

    @pytest.mark.parametrize("unit", _PROTECTED_UNITS)
    async def test_dispatcher_rejects_protected_unit_pre_systemd(self, unit: str) -> None:
        """OsNativeDispatcher rejects protected services BEFORE reaching systemd (FR-008)."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from unittest.mock import AsyncMock, patch

        dispatcher = OsNativeDispatcher()

        systemctl_mock = AsyncMock(return_value={"ok": True})
        with patch.object(dispatcher, "_run_systemctl", new=systemctl_mock):
            result = await dispatcher.execute(
                skill_name="stop_service",
                args={"unit": unit, "reason": "test"},
            )

        assert result["ok"] is False, (
            f"Protected unit '{unit}' must be rejected by denylist (pre-systemd). "
            f"Got ok=True."
        )
        assert "REJECTED_BY_POLICY" in str(result.get("reason", "")), (
            f"Reason must contain REJECTED_BY_POLICY. Got: {result.get('reason')}"
        )
        # CRITICAL: systemctl must NOT have been called for protected services.
        systemctl_mock.assert_not_awaited(), (
            f"systemctl was called for protected unit '{unit}'. "
            "Denylist must block BEFORE reaching the OS (FR-008)."
        )


# ---------------------------------------------------------------------------
# US4-C: Non-protected services pass the denylist gate
# ---------------------------------------------------------------------------


class TestNonProtectedServicesPassDenylist:
    """nginx, postgresql etc. are not blocked by the denylist."""

    @pytest.mark.parametrize("unit", ["nginx.service", "postgresql.service", "sshd.service"])
    async def test_non_protected_unit_reaches_systemctl(self, unit: str) -> None:
        """Non-protected unit passes denylist and reaches systemctl (denylist doesn't over-block)."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from unittest.mock import AsyncMock, patch

        dispatcher = OsNativeDispatcher()
        fake_result = {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
        with patch.object(
            dispatcher, "_run_systemctl", new=AsyncMock(return_value=fake_result)
        ) as systemctl_mock:
            result = await dispatcher.execute(
                skill_name="start_service",
                args={"unit": unit, "reason": "test"},
            )

        assert result["ok"] is True, (
            f"Non-protected unit '{unit}' should NOT be rejected by denylist. Got: {result}"
        )
        systemctl_mock.assert_awaited_once(), (
            f"systemctl should have been called for non-protected unit '{unit}'."
        )


# ---------------------------------------------------------------------------
# US4-D: Registry contains service mutation + scheduler skills
# ---------------------------------------------------------------------------


class TestWriteSkillsInRegistry:
    """CapabilityRegistry has bindings for all US4 WRITE/HIGH skills."""

    @pytest.mark.parametrize("skill_name", SERVICE_MUTATION_SKILLS)
    def test_service_mutation_registered_high(self, skill_name: str) -> None:
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import RiskLevel

        registry = CapabilityRegistry()
        binding = registry.resolve(skill_name)

        assert binding is not None, (
            f"'{skill_name}' not in CapabilityRegistry. Must be registered (FR-006/US4)."
        )
        assert binding.executor == "os_native", (
            f"'{skill_name}' executor must be 'os_native'. Got {binding.executor!r}"
        )
        assert binding.risk == RiskLevel.HIGH, (
            f"'{skill_name}' must have risk=HIGH (HITL mandatory). Got {binding.risk}"
        )
        assert binding.auto_executable is False, (
            f"'{skill_name}' must NOT be auto_executable (HITL mandatory). "
            f"Got {binding.auto_executable}"
        )
        assert binding.required_capability == "system_services", (
            f"'{skill_name}' must require 'system_services'. Got {binding.required_capability!r}"
        )

    @pytest.mark.parametrize("skill_name", SCHEDULER_WRITE_SKILLS)
    def test_scheduler_write_registered_high(self, skill_name: str) -> None:
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import RiskLevel

        registry = CapabilityRegistry()
        binding = registry.resolve(skill_name)

        assert binding is not None, (
            f"'{skill_name}' not in CapabilityRegistry (FR-010/US4)."
        )
        assert binding.executor == "os_native"
        assert binding.risk == RiskLevel.HIGH
        assert binding.auto_executable is False
        assert binding.required_capability == "scheduler", (
            f"'{skill_name}' must require 'scheduler'. Got {binding.required_capability!r}"
        )

    def test_list_scheduled_tasks_registered_read_only(self) -> None:
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import RiskLevel

        registry = CapabilityRegistry()
        binding = registry.resolve("list_scheduled_tasks")

        assert binding is not None, "list_scheduled_tasks not in registry (US4)."
        assert binding.executor == "os_native"
        assert binding.risk == RiskLevel.LOW
        assert binding.auto_executable is True
        assert binding.required_capability == "scheduler"


# ---------------------------------------------------------------------------
# US4-E: CONDITION-1 — _execute_via_legacy_executor is fail-closed without dispatcher
# ---------------------------------------------------------------------------


class TestLegacyExecutorIsFailClosed:
    """CONDITION-1: _execute_via_legacy_executor must return REJECTED when no dispatcher.

    The only valid execution path is through OsNativeDispatcher. Without dispatcher,
    return REJECTED result — never call asyncio.to_thread(EXECUTOR).
    """

    async def test_execute_via_legacy_executor_is_fail_closed(self) -> None:
        """_execute_via_legacy_executor returns REJECTED result (not raw executor)."""
        from hermes.shell_server.os_native_skills.tool_specs import (
            _execute_via_legacy_executor,
        )

        result = await _execute_via_legacy_executor("screenshot", {})

        assert result.get("ok") is False, (
            "_execute_via_legacy_executor must return ok=False when no dispatcher "
            "(CONDITION-1 fail-closed). Got ok=True."
        )
        assert "REJECTED" in str(result.get("reason", "")).upper(), (
            "_execute_via_legacy_executor must include 'REJECTED' in reason. "
            f"Got: {result.get('reason')}"
        )

    def test_legacy_executor_ast_no_asyncio_to_thread(self) -> None:
        """AST check: _execute_via_legacy_executor must not call asyncio.to_thread(executor)."""
        tool_specs_path = (
            Path(__file__).parent.parent.parent
            / "src/hermes/shell_server/os_native_skills/tool_specs.py"
        )
        src = tool_specs_path.read_text(encoding="utf-8")
        tree = ast.parse(src)

        to_thread_in_legacy: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if node.name == "_execute_via_legacy_executor":
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "to_thread"
                    ):
                        to_thread_in_legacy.append(getattr(child, "lineno", -1))

        assert to_thread_in_legacy == [], (
            f"_execute_via_legacy_executor at lines {to_thread_in_legacy} calls "
            "asyncio.to_thread (raw executor). This is CONDITION-1 bypass. "
            "Must return REJECTED result instead (fail-closed)."
        )


# ---------------------------------------------------------------------------
# US4-F: Extended G1 AST — ALL functions in tool_specs.py
# ---------------------------------------------------------------------------


class TestToolSpecsAllFunctionsNoRawExecutorCall:
    """Extended G1: no function in tool_specs.py calls asyncio.to_thread(EXECUTOR) directly.

    Covers EVERY function — not just _default_read_handler.
    Extends existing test_broker_os_native_route.py:TestToolSpecsDoesNotBypassBroker.
    """

    _TOOL_SPECS_PATH = (
        Path(__file__).parent.parent.parent
        / "src/hermes/shell_server/os_native_skills/tool_specs.py"
    )

    def test_no_function_calls_raw_executor_via_asyncio(self) -> None:
        """No function in tool_specs.py calls asyncio.to_thread(executor_call) directly."""
        src = self._TOOL_SPECS_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)

        violations: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            fn_name = node.name
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "to_thread"
                ):
                    # Any asyncio.to_thread in any function in this module is a violation.
                    violations.append((fn_name, getattr(child, "lineno", -1)))

        assert violations == [], (
            f"tool_specs.py functions {violations} call asyncio.to_thread directly. "
            "All execution must route via OsNativeDispatcher (G1 / CONDITION-1). "
            "Never call raw EXECUTORS from this module."
        )


# ---------------------------------------------------------------------------
# US4-G: CONDITION-2 — denylist resolves via systemctl show for real aliases
# ---------------------------------------------------------------------------


class TestDenylistCanonicalIdentityResolution:
    """CONDITION-2: ProtectedServiceDenylist resolves via systemctl show -p Id,Names."""

    def test_is_protected_with_systemctl_show_resolution(self) -> None:
        """is_protected_with_systemctl_show returns True for aliases resolved by systemd."""
        from hermes.capabilities.infrastructure.protected_service_denylist import (
            ProtectedServiceDenylist,
        )

        denylist = ProtectedServiceDenylist()

        # The method must exist and be callable.
        assert hasattr(denylist, "is_protected_canonical"), (
            "ProtectedServiceDenylist must have 'is_protected_canonical' method "
            "for CONDITION-2 systemctl show resolution."
        )

    def test_canonical_check_runs_without_systemd(self) -> None:
        """is_protected_canonical fallback: works without systemd (lexical only)."""
        from hermes.capabilities.infrastructure.protected_service_denylist import (
            ProtectedServiceDenylist,
        )

        denylist = ProtectedServiceDenylist()

        # The canonical check must at minimum do lexical resolution (fall-through to base).
        result = denylist.is_protected_canonical("hermes-runtime.service")

        assert result is True, (
            "is_protected_canonical must return True for 'hermes-runtime.service' "
            "(lexical fallback without systemd)."
        )

    def test_canonical_check_uses_systemctl_show_when_available(self) -> None:
        """is_protected_canonical uses systemctl show when systemd is available (CONDITION-2)."""
        from hermes.capabilities.infrastructure.protected_service_denylist import (
            ProtectedServiceDenylist,
        )
        from unittest.mock import patch

        denylist = ProtectedServiceDenylist()

        # Simulate systemctl show returning an alias that maps to a protected service.
        # For example: "hermes-rt" could be an alias for "hermes-runtime.service".
        fake_show_output = "Id=hermes-runtime.service\nNames=hermes-runtime.service hermes-rt.service\n"

        with patch.object(
            denylist,
            "_systemctl_show_id_names",
            return_value=fake_show_output,
            create=True,
        ):
            result = denylist.is_protected_canonical("hermes-rt")

        assert result is True, (
            "is_protected_canonical must detect 'hermes-rt' as protected via "
            "systemctl show Id,Names resolution (CONDITION-2)."
        )

    def test_canonical_check_fail_closed_on_resolution_error_for_ambiguous_unit(self) -> None:
        """is_protected_canonical is fail-closed for empty/ambiguous canonicalization.

        When systemctl show fails AND the lexical fallback produces an empty
        canonical name (truly indeterminate identity), the denylist must
        treat the unit as protected (fail-closed, NFR-002).
        """
        from hermes.capabilities.infrastructure.protected_service_denylist import (
            ProtectedServiceDenylist,
        )
        from unittest.mock import patch

        denylist = ProtectedServiceDenylist()

        # Simulate both systemctl show failing AND an empty-string unit name.
        with patch.object(
            denylist,
            "_systemctl_show_id_names",
            side_effect=Exception("systemd unavailable"),
            create=True,
        ):
            # Empty string: truly indeterminate → fail-closed (is_protected("") returns True).
            result = denylist.is_protected_canonical("")

        # Fail-closed: empty canonical = cannot determine identity = deny.
        assert result is True, (
            "is_protected_canonical must be fail-closed for empty/indeterminate unit name. "
            "NFR-002: when resolution cannot determine identity, treat as protected."
        )

    def test_canonical_check_systemctl_fail_falls_back_to_lexical_for_known_protected(self) -> None:
        """When systemctl show fails, lexical fallback still catches known protected names."""
        from hermes.capabilities.infrastructure.protected_service_denylist import (
            ProtectedServiceDenylist,
        )
        from unittest.mock import patch

        denylist = ProtectedServiceDenylist()

        with patch.object(
            denylist,
            "_systemctl_show_id_names",
            side_effect=Exception("systemd unavailable"),
            create=True,
        ):
            # Known protected service — lexical fallback must still catch it.
            result = denylist.is_protected_canonical("hermes-runtime.service")

        assert result is True, (
            "When systemctl show fails, lexical fallback must still protect "
            "known protected services like 'hermes-runtime.service'."
        )


# ---------------------------------------------------------------------------
# US4-H: Kill-switch (pause) applies to WRITE skills (FR-011)
# ---------------------------------------------------------------------------


class TestKillSwitchAppliesToWriteSkills:
    """Agent pause applies to service mutation skills (FR-011 / US4 AC5)."""

    @pytest.mark.parametrize("skill_name", SERVICE_MUTATION_SKILLS)
    async def test_paused_agent_blocks_service_mutation(self, skill_name: str) -> None:
        """With agent paused, service mutation → REJECTED_BY_POLICY (FR-011)."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import SurfaceAdapterDispatcher

        class _PausedState:
            async def is_paused(self) -> bool:
                return True

        mgr = _make_consent_manager_with_system_services()
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name=skill_name,
            surface_kind=None,
            required_capability="system_services",
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

        broker = CapabilityBroker(
            registry=reg,
            consent_manager=mgr,
            approval_gate=gate,
            dispatcher=surface_dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=intent_log,
            os_native_dispatcher=os_disp,
            agent_state=_PausedState(),  # type: ignore[arg-type]
        )

        outcome = await broker.dispatch(
            _proposal(skill_name, {"unit": "nginx.service", "reason": "test"}),
            _ctx(),
            hitl_approval_token="any-token",
        )

        assert outcome.status == ExecutionStatus.REJECTED_BY_POLICY, (
            f"Paused agent must block {skill_name}. Got {outcome.status}: {outcome.error}"
        )
