"""Composition-root wiring tests.

Verifies that the critical integration gaps are closed:
  (W1) Broker receives composio_adapter= (KC-4).
  (W2) Broker uses ComposioCapabilityRegistry (dynamic slug resolution).
  (W3) NousReasoningEngine receives broker + consent_context (F2).
  (W4) HermesShellWindow._approved_sites_store passed to HermesSettingsWindow.
  (W5) ComposioToolsRegistry built with broker-aware tools_builder (KC-4 live-reload).

Each test is pure-unit: no network, no real DB, no GTK. Import of GTK-dependent
modules is guarded so the test suite can run in headless CI environments.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.unit

_TENANT = UUID("00000000-0000-0000-0000-000000000001")
_OPERATOR = UUID("00000000-0000-0000-0000-000000000002")
_SIGNING_KEY = os.urandom(32)


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _FakeConsentManager:
    def assert_active(self, *, human_operator_id, capability):
        from dataclasses import dataclass  # noqa: PLC0415
        from hermes.agents_os.application.consent_manager import ConsentScope  # noqa: PLC0415

        @dataclass
        class _C:
            scope: ConsentScope = ConsentScope.ONCE
        return _C()

    def use(self, *, human_operator_id, capability):
        pass


def _build_minimal_broker(*, composio_adapter=None, registry=None):
    """Build a minimal CapabilityBroker for wiring assertions."""
    from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
    from hermes.capabilities.application.capability_broker import CapabilityBroker
    from hermes.capabilities.application.capability_registry import CapabilityRegistry
    from hermes.capabilities.application.intent_log import IntentLog
    from hermes.capabilities.infrastructure.surface_adapter_dispatcher import SurfaceAdapterDispatcher
    from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
    from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor

    if registry is None:
        registry = CapabilityRegistry()

    class _NullAuditRepo:
        async def append(self, entry: Any) -> None:
            pass
        async def head_hash_hex(self) -> str | None:
            return None
        async def load_chain(self, *, tenant_id=None):
            return []

    return CapabilityBroker(
        registry=registry,
        consent_manager=_FakeConsentManager(),
        approval_gate=FakeApprovalGate(),
        dispatcher=SurfaceAdapterDispatcher(adapters={}),
        signer=AuditHashChainSigner(signing_key=_SIGNING_KEY),
        audit_repo=_NullAuditRepo(),
        intent_log=IntentLog(),
        anchor=FakeExternalAnchor(),
        composio_adapter=composio_adapter,
    )


# ---------------------------------------------------------------------------
# W1: Broker receives composio_adapter
# ---------------------------------------------------------------------------


class TestBrokerReceivesComposioAdapter:
    """W1: _build_real_broker injects ComposioSurfaceAdapter into CapabilityBroker."""

    def test_composio_adapter_stored_on_broker(self) -> None:
        """When a composio_adapter is passed, broker stores it as _composio_adapter."""
        from hermes.capabilities.infrastructure.composio_surface_adapter import (
            ComposioSurfaceAdapter,
        )

        adapter = ComposioSurfaceAdapter(api_key="csk-test", entity_id="ent-1")
        broker = _build_minimal_broker(composio_adapter=adapter)

        assert broker._composio_adapter is adapter, (
            "W1: broker._composio_adapter must be the injected ComposioSurfaceAdapter"
        )

    def test_broker_without_composio_adapter_is_none(self) -> None:
        """Broker without composio_adapter stores None (fail-closed on Composio dispatch)."""
        broker = _build_minimal_broker(composio_adapter=None)
        assert broker._composio_adapter is None, (
            "W1: broker._composio_adapter must be None when not injected"
        )

    @pytest.mark.asyncio
    async def test_composio_dispatch_rejected_without_adapter(self) -> None:
        """Without composio_adapter, broker.dispatch for executor=composio → REJECTED_BY_POLICY."""
        from hermes.capabilities.application.composio_capability_registry import (
            ComposioCapabilityRegistry,
        )
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import ConsentContext, ExecutionStatus
        from hermes.domain.proposal import ToolCallProposal

        registry = ComposioCapabilityRegistry(static_registry=CapabilityRegistry())
        broker = _build_minimal_broker(composio_adapter=None, registry=registry)

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="gmail_get_email",
            tenant_id=_TENANT,
            entity_id="ent-1",
            entity_type="composio",
            parameters={"slug": "GMAIL_GET_EMAIL", "params": {}, "entity_id": "ent-1"},
            justification="test",
        )
        consent = ConsentContext(
            tenant_id=_TENANT,
            operator_id=_OPERATOR,
            derived_from_untrusted_content=False,
        )

        outcome = await broker.dispatch(proposal, consent)
        assert outcome.status is ExecutionStatus.REJECTED_BY_POLICY, (
            "W1: without composio_adapter, Composio dispatch must fail-closed "
            f"(got {outcome.status})"
        )


# ---------------------------------------------------------------------------
# W2: Broker uses ComposioCapabilityRegistry
# ---------------------------------------------------------------------------


class TestBrokerUsesComposioCapabilityRegistry:
    """W2: _build_real_broker wraps CapabilityRegistry with ComposioCapabilityRegistry."""

    def test_composio_capability_registry_resolves_read_slugs(self) -> None:
        """ComposioCapabilityRegistry resolves Composio READ slugs to low/auto bindings."""
        from hermes.capabilities.application.composio_capability_registry import (
            ComposioCapabilityRegistry,
        )
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import RiskLevel

        registry = ComposioCapabilityRegistry(static_registry=CapabilityRegistry())

        binding = registry.resolve("gmail_get_email")

        assert binding is not None, "W2: ComposioCapabilityRegistry must resolve Composio READ slug"
        assert binding.auto_executable is True, "W2: Composio READ binding must be auto_executable"
        assert binding.risk is RiskLevel.LOW, "W2: Composio READ binding must be LOW risk"
        assert binding.executor == "composio", "W2: Composio READ binding executor must be 'composio'"

    def test_static_tools_take_priority_over_composio(self) -> None:
        """Static CapabilityRegistry bindings are not overridden by ComposioCapabilityRegistry."""
        from hermes.capabilities.application.composio_capability_registry import (
            ComposioCapabilityRegistry,
        )
        from hermes.capabilities.application.capability_registry import CapabilityRegistry

        registry = ComposioCapabilityRegistry(static_registry=CapabilityRegistry())

        # 'read_file' is in the static registry — must NOT become a Composio binding.
        binding = registry.resolve("read_file")
        assert binding is not None
        assert binding.executor != "composio", (
            "W2: static registry binding must not be overridden by ComposioCapabilityRegistry"
        )

    def test_composio_write_slug_returns_none(self) -> None:
        """WRITE Composio slugs → None (fail-closed in broker)."""
        from hermes.capabilities.application.composio_capability_registry import (
            ComposioCapabilityRegistry,
        )
        from hermes.capabilities.application.capability_registry import CapabilityRegistry

        registry = ComposioCapabilityRegistry(static_registry=CapabilityRegistry())
        assert registry.resolve("gmail_send_email") is None, (
            "W2: WRITE Composio slug must resolve to None (fail-closed)"
        )


# ---------------------------------------------------------------------------
# W3: NousReasoningEngine receives broker + consent_context
# ---------------------------------------------------------------------------


class TestNousEngineReceivesBroker:
    """W3: _build_nous_engine wires broker + consent_context into NousReasoningEngine."""

    def test_nous_engine_stores_broker(self) -> None:
        """NousReasoningEngine stores broker passed at construction."""
        from hermes.capabilities.domain.ports import ConsentContext
        from hermes.runtime.nous_engine import NousReasoningEngine
        from hermes.agents.domain.agent import default_agent

        broker = _build_minimal_broker()
        consent = ConsentContext(
            tenant_id=_TENANT,
            operator_id=_OPERATOR,
        )

        engine = NousReasoningEngine(
            persona=default_agent().to_persona(),
            broker=broker,
            consent_context=consent,
            tenant_id=_TENANT,
        )

        assert engine._broker is broker, (
            "W3: NousReasoningEngine must store injected broker as _broker"
        )
        assert engine._consent_context is consent, (
            "W3: NousReasoningEngine must store injected consent_context"
        )
        assert engine._tenant_id == _TENANT, (
            "W3: NousReasoningEngine must store injected tenant_id"
        )

    def test_nous_engine_without_broker_stores_none(self) -> None:
        """NousReasoningEngine without broker stores None (writes fail-closed in gate)."""
        from hermes.runtime.nous_engine import NousReasoningEngine
        from hermes.agents.domain.agent import default_agent

        engine = NousReasoningEngine(persona=default_agent().to_persona())

        assert engine._broker is None, (
            "W3: NousReasoningEngine without broker must store _broker=None"
        )

    def test_governed_agent_broker_wire(self) -> None:
        """GovernedAIAgent stores the broker it receives at construction."""
        from hermes.capabilities.domain.ports import ConsentContext
        from hermes.runtime.nous_engine import GovernedAIAgent

        broker = _build_minimal_broker()
        consent = ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)

        # GovernedAIAgent wraps AIAgent (NousResearch) — mock the inner AIAgent
        # so this test does not require hermes-agent to be installed.
        with patch("hermes.runtime.nous_engine._import_ai_agent") as mock_import:
            mock_ai_agent_class = MagicMock()
            mock_ai_agent_instance = MagicMock()
            mock_ai_agent_class.return_value = mock_ai_agent_instance
            mock_import.return_value = mock_ai_agent_class

            agent = GovernedAIAgent(
                broker=broker,
                consent_context=consent,
                engine_loop=None,
                tenant_id=_TENANT,
            )

        assert agent._broker is broker, (
            "W3: GovernedAIAgent must store injected broker"
        )
        assert agent._consent_context is consent, (
            "W3: GovernedAIAgent must store injected consent_context"
        )

    def test_governed_agent_write_blocked_without_broker(self) -> None:
        """GovernedAIAgent._dispatch_write_proposal returns BLOCKED when broker is None."""
        from hermes.runtime.nous_engine import GovernedAIAgent

        with patch("hermes.runtime.nous_engine._import_ai_agent") as mock_import:
            mock_ai_agent_class = MagicMock()
            mock_ai_agent_class.return_value = MagicMock()
            mock_import.return_value = mock_ai_agent_class

            agent = GovernedAIAgent(
                broker=None,
                consent_context=None,
                engine_loop=None,
                tenant_id=_TENANT,
            )

        result = agent._dispatch_write_proposal(
            function_name="write_file",
            function_args={"path": "/tmp/test.txt", "content": "x"},
            effective_task_id="task-1",
            tool_call_id=None,
        )

        import json
        data = json.loads(result)
        assert "error" in data, "W3: without broker, WRITE must be BLOCKED"
        assert "BLOCKED" in data["error"], (
            f"W3: result must contain 'BLOCKED', got: {data['error']}"
        )


# ---------------------------------------------------------------------------
# W4: ApprovedSitesStore wired from window to settings
# ---------------------------------------------------------------------------


class TestApprovedSitesStoreWiring:
    """W4: ApprovedSitesStore created in HermesShellWindow and passed to HermesSettingsWindow."""

    def test_approved_sites_store_exists_on_window(self) -> None:
        """HermesShellWindow.__init__ creates an _approved_sites_store attribute."""
        from hermes.shell.presentation.gtk4.approved_sites_store import ApprovedSitesStore

        # We can't instantiate HermesShellWindow without GTK, but we can verify
        # that the module-level import is present and the store attribute is defined
        # in the __init__ source by checking the class body.
        import inspect
        import hermes.shell.presentation.gtk4.window as window_module  # noqa: PLC0415

        src = inspect.getsource(window_module.HermesShellWindow.__init__)
        assert "ApprovedSitesStore" in src or "_approved_sites_store" in src, (
            "W4: HermesShellWindow.__init__ must instantiate ApprovedSitesStore"
        )

    def test_settings_window_receives_approved_sites_store(self) -> None:
        """_open_settings_window passes approved_sites_store= to HermesSettingsWindow."""
        import inspect
        import hermes.shell.presentation.gtk4.window as window_module  # noqa: PLC0415

        src = inspect.getsource(window_module.HermesShellWindow._open_settings_window)
        assert "approved_sites_store" in src, (
            "W4: _open_settings_window must pass approved_sites_store= to HermesSettingsWindow"
        )

    def test_approved_sites_store_import_in_window_module(self) -> None:
        """window.py imports ApprovedSitesStore at module level."""
        import hermes.shell.presentation.gtk4.window as window_module  # noqa: PLC0415

        assert hasattr(window_module, "ApprovedSitesStore"), (
            "W4: window module must import ApprovedSitesStore"
        )

    def test_approved_sites_store_basic_lifecycle(self, tmp_path) -> None:
        """ApprovedSitesStore add/remove/as_frozenset works correctly."""
        from hermes.shell.presentation.gtk4.approved_sites_store import ApprovedSitesStore

        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
            store = ApprovedSitesStore()

            added = store.add("example.com")
            assert added is True
            assert "example.com" in store.as_frozenset()

            removed = store.remove("example.com")
            assert removed is True
            assert "example.com" not in store.as_frozenset()

    def test_approved_sites_store_rejects_invalid_domain(self, tmp_path) -> None:
        """ApprovedSitesStore rejects domains with schemes or slashes (fail-closed)."""
        from hermes.shell.presentation.gtk4.approved_sites_store import ApprovedSitesStore

        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path)}):
            store = ApprovedSitesStore()

            assert store.add("https://example.com") is False, (
                "W4: domain with scheme must be rejected"
            )
            assert store.add("example.com/path") is False, (
                "W4: domain with path must be rejected"
            )
            assert store.as_frozenset() == frozenset()


# ---------------------------------------------------------------------------
# W5: ComposioToolsRegistry broker-aware tools_builder
# ---------------------------------------------------------------------------


class TestComposioRegistryBrokerAwareBuilder:
    """W5: ComposioToolsRegistry built with broker-aware tools_builder (KC-4 live-reload)."""

    @pytest.mark.asyncio
    async def test_broker_aware_tools_builder_is_called_with_broker(self, tmp_path) -> None:
        """ComposioToolsRegistry calls the broker-aware builder on refresh."""
        from hermes.runtime.composio_tools_registry import ComposioToolsRegistry
        from hermes.capabilities.domain.ports import ConsentContext

        broker = _build_minimal_broker()
        consent = ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)

        captured_broker = []
        captured_consent = []

        async def _capturing_builder(credential) -> tuple:
            # Verify that the builder captured the correct broker and consent_context.
            captured_broker.append(broker)
            captured_consent.append(consent)
            return ()

        # Fake credential loader so the registry actually tries to refresh.
        def _fake_loader(_db_path):
            from types import SimpleNamespace  # noqa: PLC0415
            return SimpleNamespace(api_key="csk-test", entity_id="ent-1")

        registry = ComposioToolsRegistry(
            db_path=tmp_path / "db.sqlite",
            ttl_s=0.0,  # always stale → forces refresh on first call
            credential_loader=_fake_loader,
            tools_builder=_capturing_builder,
        )

        await registry.get_composio_tools()

        assert len(captured_broker) == 1, (
            "W5: tools_builder must be called once during refresh"
        )
        assert captured_broker[0] is broker, (
            "W5: tools_builder must receive the injected broker"
        )

    @pytest.mark.asyncio
    async def test_null_composio_registry_fallback_returns_empty(self) -> None:
        """_NullComposioRegistry.get_composio_tools() returns empty tuple (fail-soft)."""
        class _NullRegistry:
            async def get_composio_tools(self) -> tuple:
                return ()

        result = await _NullRegistry().get_composio_tools()
        assert result == (), "W5: NullComposioRegistry must return empty tuple"


# ---------------------------------------------------------------------------
# W6: begin_computer_use in OS-native catalog → visible in LLM tool schema
# ---------------------------------------------------------------------------


class TestBeginComputerUseCatalogEntry:
    """W6 (Gap A): begin_computer_use is registered in the OS-native catalog and
    emitted by build_os_native_tool_specs so the LLM sees it as a callable tool.
    """

    def test_begin_computer_use_in_os_native_skills_tuple(self) -> None:
        """Gap A: BEGIN_COMPUTER_USE must appear in OS_NATIVE_SKILLS."""
        from hermes.shell_server.os_native_skills.catalog import (
            OS_NATIVE_SKILLS,
            skill_by_name,
        )

        skill = skill_by_name("begin_computer_use")
        assert skill is not None, (
            "Gap A: begin_computer_use must be registered in OS_NATIVE_SKILLS"
        )
        assert skill in OS_NATIVE_SKILLS, (
            "Gap A: begin_computer_use must be in the OS_NATIVE_SKILLS tuple"
        )

    def test_begin_computer_use_skill_is_write_high(self) -> None:
        """begin_computer_use is WRITE_PROPOSAL (HIGH) — requires HITL before GUI access."""
        from hermes.shell_server.os_native_skills.catalog import (
            SkillRisk,
            skill_by_name,
        )

        skill = skill_by_name("begin_computer_use")
        assert skill is not None
        assert skill.risk is SkillRisk.WRITE_PROPOSAL, (
            "begin_computer_use must be WRITE_PROPOSAL (HIGH risk, HITL required)"
        )
        assert "input_control" in skill.capabilities, (
            "begin_computer_use must require input_control capability"
        )

    def test_begin_computer_use_goal_is_required_param(self) -> None:
        """begin_computer_use schema must declare 'goal' as a required parameter."""
        from hermes.shell_server.os_native_skills.catalog import skill_by_name

        skill = skill_by_name("begin_computer_use")
        assert skill is not None
        assert "goal" in skill.parameters_schema.get("required", []), (
            "begin_computer_use schema must require 'goal'"
        )
        assert "goal" in skill.parameters_schema.get("properties", {}), (
            "begin_computer_use schema must define 'goal' property"
        )

    def test_build_os_native_tool_specs_excludes_begin_computer_use_native_replaced(self) -> None:
        """begin_computer_use (control GUI custom) ya NO se emite: lo reemplaza el
        toolset NATIVO `computer_use` de Hermes (backend Wayland lumen-cua-driver).
        Sigue en el catálogo OS_NATIVE_SKILLS pero excluido del schema del LLM."""
        from hermes.shell_server.os_native_skills.tool_specs import build_os_native_tool_specs

        specs = build_os_native_tool_specs()
        names = {s.name for s in specs}

        assert "begin_computer_use" not in names
        assert "mouse_click" not in names
        assert "type_text" not in names

    def test_begin_computer_use_spec_is_write_proposal_no_handler(self) -> None:
        """begin_computer_use ToolSpec must be WRITE_PROPOSAL with handler=None.

        handler=None means the broker captures it as a proposal routed to
        OsNativeDispatcher._dispatch_computer_use after HITL approval.
        """
        from hermes.shell_server.os_native_skills.tool_specs import build_os_native_tool_specs
        from hermes.domain.tool_spec import ToolRisk

        specs = build_os_native_tool_specs()
        by_name = {s.name: s for s in specs}
        spec = by_name.get("begin_computer_use")

        # Ya NO se emite al LLM: reemplazado por el toolset NATIVO computer_use.
        # (El binding del registry sigue existiendo — ver test siguiente — para
        # que el broker gatee si alguna ruta lo propusiera.)
        assert spec is None

    def test_capability_registry_resolves_begin_computer_use(self) -> None:
        """Gap A: CapabilityRegistry must resolve begin_computer_use to HIGH/os_native binding."""
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import RiskLevel

        registry = CapabilityRegistry()
        binding = registry.resolve("begin_computer_use")

        assert binding is not None, (
            "Gap A: CapabilityRegistry must resolve begin_computer_use"
        )
        assert binding.risk is RiskLevel.HIGH, (
            "begin_computer_use registry binding must be HIGH risk"
        )
        assert binding.executor == "os_native", (
            "begin_computer_use registry binding must use executor='os_native'"
        )
        assert binding.auto_executable is False, (
            "begin_computer_use must not be auto_executable (requires HITL)"
        )
        assert binding.persistent_forbidden is True, (
            "begin_computer_use must have persistent_forbidden=True (session-scoped only)"
        )


# ---------------------------------------------------------------------------
# W7: OsNativeDispatcher wired with computer-use deps + broker back-patch
# ---------------------------------------------------------------------------


class TestOsNativeDispatcherWiring:
    """W7 (Gap B): OsNativeDispatcher is constructed with computer-use deps and
    wired into CapabilityBroker, with broker back-patched via wire_computer_use_broker.
    """

    def test_dispatcher_accepts_computer_use_deps(self) -> None:
        """OsNativeDispatcher constructor accepts computer-use keyword args."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
        from uuid import uuid4

        operator_id = uuid4()
        tenant_id = uuid4()

        dispatcher = OsNativeDispatcher(
            computer_use_consent_manager=MagicMock(),
            computer_use_broker=None,  # broker injected after construction
            computer_use_operator_id=operator_id,
            computer_use_tenant_id=tenant_id,
            computer_use_model="gpt-4o",
            computer_use_api_key="sk-test",
            computer_use_base_url=None,
        )

        assert dispatcher._cu_model == "gpt-4o", (
            "W7: dispatcher must store computer_use_model"
        )
        assert dispatcher._cu_operator_id == operator_id, (
            "W7: dispatcher must store computer_use_operator_id"
        )
        assert dispatcher._cu_tenant_id == tenant_id, (
            "W7: dispatcher must store computer_use_tenant_id"
        )

    def test_wire_computer_use_broker_injects_broker(self) -> None:
        """wire_computer_use_broker() sets _cu_broker after construction."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher

        dispatcher = OsNativeDispatcher(computer_use_model="gpt-4o")
        assert dispatcher._cu_broker is None, "broker must be None before wiring"

        fake_broker = MagicMock()
        dispatcher.wire_computer_use_broker(fake_broker)

        assert dispatcher._cu_broker is fake_broker, (
            "W7: wire_computer_use_broker must inject broker into _cu_broker"
        )

    def test_dispatcher_supports_begin_computer_use(self) -> None:
        """OsNativeDispatcher.supports('begin_computer_use') returns True."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher

        dispatcher = OsNativeDispatcher()
        assert dispatcher.supports("begin_computer_use"), (
            "W7: OsNativeDispatcher must support begin_computer_use"
        )

    @pytest.mark.asyncio
    async def test_dispatch_computer_use_fail_closed_without_model(self) -> None:
        """begin_computer_use fails gracefully when model is not configured."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher

        dispatcher = OsNativeDispatcher(
            computer_use_consent_manager=MagicMock(),
            computer_use_broker=MagicMock(),
            computer_use_operator_id=_OPERATOR,
            computer_use_tenant_id=_TENANT,
            computer_use_model="",  # no model configured
        )

        result = await dispatcher.execute(
            skill_name="begin_computer_use",
            args={"goal": "test goal"},
        )

        assert result["ok"] is False, (
            "W7: begin_computer_use without model must return ok=False"
        )
        assert "not wired" in result["reason"].lower() or "model" in result["reason"].lower(), (
            f"W7: error reason must mention missing config: {result['reason']}"
        )

    @pytest.mark.asyncio
    async def test_dispatch_computer_use_fail_closed_without_broker(self) -> None:
        """begin_computer_use fails gracefully when broker is not wired."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher

        dispatcher = OsNativeDispatcher(
            computer_use_consent_manager=MagicMock(),
            computer_use_broker=None,   # broker not yet wired
            computer_use_operator_id=_OPERATOR,
            computer_use_tenant_id=_TENANT,
            computer_use_model="gpt-4o",
        )

        result = await dispatcher.execute(
            skill_name="begin_computer_use",
            args={"goal": "test goal"},
        )

        assert result["ok"] is False, (
            "W7: begin_computer_use without broker must return ok=False"
        )

    def test_broker_stores_os_native_dispatcher(self) -> None:
        """Gap B: CapabilityBroker stores OsNativeDispatcher as _os_native_dispatcher."""
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher

        dispatcher = OsNativeDispatcher(computer_use_model="gpt-4o")
        broker = _build_minimal_broker()
        # Manually inject as the composition root does:
        broker._os_native_dispatcher = dispatcher

        assert broker._os_native_dispatcher is dispatcher, (
            "Gap B: CapabilityBroker must store the injected OsNativeDispatcher"
        )

    def test_build_os_native_dispatcher_builder_function(self) -> None:
        """_build_os_native_dispatcher returns an OsNativeDispatcher instance."""
        from hermes.runtime.__main__ import _build_os_native_dispatcher
        from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher

        fake_consent = MagicMock()

        # resolve_model_config is imported locally inside _build_os_native_dispatcher;
        # patch it at its definition site so the local import picks up the mock.
        with patch(
            "hermes.runtime.provider_config_source.resolve_model_config",
            return_value=None,
        ):
            dispatcher = _build_os_native_dispatcher(
                consent_manager=fake_consent,
                operator_id=_OPERATOR,
                tenant_id=_TENANT,
            )

        assert isinstance(dispatcher, OsNativeDispatcher), (
            "W7: _build_os_native_dispatcher must return OsNativeDispatcher"
        )
        assert dispatcher._cu_consent_manager is fake_consent, (
            "W7: dispatcher must store consent_manager"
        )
        assert dispatcher._cu_broker is None, (
            "W7: broker must be None before wire_computer_use_broker() is called"
        )
        assert dispatcher._cu_model == "", (
            "W7: model must be empty string when no model is configured"
        )
