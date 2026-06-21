"""Tests de seguridad F2: _tool_gate del NousReasoningEngine.

Lógica pura — sin ejecutar el agente real de Nous ni el broker real.
No requiere hermes-agent instalado.

Cobertura de los 6 requisitos del diseño F2:
  (a) WRITE → ToolCallProposal construido + broker.dispatch llamado;
              handler nativo de Nous NO invocado.
  (b) Tool desconocida → BLOCKED fail-closed; nada ejecuta.
  (c) READ → ejecuta nativo; broker NO invocado.
  (d) EXECUTED_OK → devuelve resultado real como JSON.
      REJECTED_BY_POLICY / REJECTED_BY_CONSENT → "BLOCKED" en el resultado.
      PENDING_APPROVAL → "BLOCKED" + proposal acumulada en _pending_proposals.
  (e) Puente async: broker en otro loop → resultado correcto sin deadlock.
  (f) Cobertura del catálogo: ninguna tool del catálogo de Nous queda sin
      clasificar (test falla si se añade una tool al catálogo sin clasificarla).
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
from hermes.domain.proposal import ToolCallProposal
from hermes.runtime.nous_engine import (
    GovernedAIAgent,
    _build_proposal,
    _dispatch_via_bridge,
    _is_external_content_tool,
)
from hermes.runtime.nous_tool_risk_map import (
    NOUS_TOOL_CATALOG,
    NousRisk,
    classify_nous_tool,
)

pytestmark = pytest.mark.unit

_TENANT = UUID("00000000-0000-0000-0000-000000000001")
_OPERATOR = UUID("00000000-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Fixtures compartidos
# ---------------------------------------------------------------------------


def _consent_ctx(derived: bool = False) -> Any:
    """ConsentContext de prueba (estructural, no importa el tipo exacto)."""
    from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415
    return ConsentContext(
        tenant_id=_TENANT,
        operator_id=_OPERATOR,
        derived_from_untrusted_content=derived,
    )


def _outcome(status: ExecutionStatus, result: dict | None = None, error: str | None = None) -> ExecutionOutcome:
    return ExecutionOutcome(
        proposal_id=uuid4(),
        status=status,
        result=result or {},
        error=error,
    )


def _make_governed_agent(
    broker: Any = None,
    consent_ctx: Any = None,
    engine_loop: asyncio.AbstractEventLoop | None = None,
) -> GovernedAIAgent:
    """Construye GovernedAIAgent con mocks sin importar hermes-agent."""
    fake_inner = MagicMock()

    with patch("hermes.runtime.nous_engine._import_ai_agent") as mock_import:
        mock_ai_cls = MagicMock(return_value=fake_inner)
        mock_import.return_value = mock_ai_cls
        agent = GovernedAIAgent(
            model="test/model",
            broker=broker,
            consent_context=consent_ctx or _consent_ctx(),
            engine_loop=engine_loop,
            tenant_id=_TENANT,
        )

    # El inner ya tiene _invoke_tool parcheado en __init__; reemplazamos el mock.
    agent._inner = fake_inner
    return agent


# ---------------------------------------------------------------------------
# (a) WRITE → proposal construida + broker llamado; handler nativo NO invocado
# ---------------------------------------------------------------------------


class TestWritePathDispatchesToBroker:
    def test_write_tool_builds_proposal_and_calls_broker(self) -> None:
        """Una tool WRITE construye ToolCallProposal y llama broker.dispatch."""
        broker_outcome = _outcome(ExecutionStatus.EXECUTED, result={"ok": True})
        mock_broker = MagicMock()

        # Ejecutar el puente sync: simular que broker.dispatch retorna el outcome.
        dispatch_calls: list[ToolCallProposal] = []

        def fake_dispatch_bridge(*, proposal, broker, consent_context, engine_loop):
            dispatch_calls.append(proposal)
            return broker_outcome

        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(broker=mock_broker, engine_loop=loop)

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=fake_dispatch_bridge):
            result_str = agent._invoke_tool(
                "write_file",
                {"path": "/tmp/test.txt", "content": "hello"},
                "task-001",
                "call-001",
            )

        # Proposal construida correctamente.
        assert len(dispatch_calls) == 1
        proposal = dispatch_calls[0]
        assert proposal.tool_name == "write_file"
        assert proposal.tenant_id == _TENANT
        assert proposal.parameters == {"path": "/tmp/test.txt", "content": "hello"}

        # Resultado: EXECUTED → JSON con resultado real.
        parsed = json.loads(result_str)
        assert parsed.get("ok") is True
        loop.close()

    def test_write_tool_never_calls_native_invoke(self) -> None:
        """Para WRITE, invoke_tool nativo de Nous NUNCA se invoca."""
        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(engine_loop=loop)

        native_called = []

        def fake_native(*args, **kwargs):
            native_called.append(True)
            return json.dumps({"native": "executed"})

        broker_outcome = _outcome(ExecutionStatus.EXECUTED, result={"ok": True})

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", return_value=broker_outcome):
            with patch("hermes.runtime.nous_engine.GovernedAIAgent._call_native_invoke", side_effect=fake_native):
                agent._invoke_tool("write_file", {}, "task-001")

        assert len(native_called) == 0, "handler nativo fue invocado para WRITE — violación de la garantía"
        loop.close()

    def test_write_tool_never_calls_native_for_terminal(self) -> None:
        """terminal (HIGH) tampoco invoca el handler nativo."""
        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(engine_loop=loop)
        native_called = []

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", return_value=_outcome(ExecutionStatus.EXECUTED)):
            with patch("hermes.runtime.nous_engine.GovernedAIAgent._call_native_invoke", lambda *a, **kw: native_called.append(1) or ""):
                agent._invoke_tool("terminal", {"command": "rm -rf /"}, "task-x")

        assert not native_called
        loop.close()


# ---------------------------------------------------------------------------
# (b) Tool desconocida → BLOCKED fail-closed
# ---------------------------------------------------------------------------


class TestUnknownToolFailClosed:
    def test_unknown_tool_returns_blocked(self) -> None:
        """Una tool no catalogada devuelve BLOCKED sin ejecutar nada."""
        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(engine_loop=loop)
        native_called = []

        with patch("hermes.runtime.nous_engine.GovernedAIAgent._call_native_invoke", lambda *a, **kw: native_called.append(1) or ""):
            with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=AssertionError("no debería llamar broker")):
                result_str = agent._invoke_tool("unknown_mystery_tool_xyz", {}, "task-001")

        parsed = json.loads(result_str)
        assert "BLOCKED" in parsed.get("error", "")
        assert not native_called
        loop.close()

    def test_unknown_tool_does_not_call_broker(self) -> None:
        """Una tool desconocida NO llama al broker (fail-closed puro)."""
        loop = asyncio.new_event_loop()
        broker = MagicMock()
        agent = _make_governed_agent(broker=broker, engine_loop=loop)

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=AssertionError) as mock_bridge:
            agent._invoke_tool("completely_unknown_tool", {}, "task-001")

        mock_bridge.assert_not_called()
        loop.close()

    def test_mcp_dynamic_tool_blocked(self) -> None:
        """Herramientas MCP dinámicas (no en el catálogo) se bloquean."""
        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(engine_loop=loop)

        result_str = agent._invoke_tool("mcp__some_server__some_tool", {}, "task-001")

        parsed = json.loads(result_str)
        assert "BLOCKED" in parsed["error"]
        loop.close()


# ---------------------------------------------------------------------------
# (c) READ → ejecuta nativo; broker NO invocado
# ---------------------------------------------------------------------------


class TestReadPathExecutesNative:
    def test_read_tool_calls_native_invoke(self) -> None:
        """Una tool READ ejecuta el handler nativo de Nous."""
        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(engine_loop=loop)

        native_result = json.dumps({"content": "file content"})

        with patch.object(agent, "_call_native_invoke", return_value=native_result) as mock_native:
            with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=AssertionError) as mock_bridge:
                result = agent._invoke_tool("read_file", {"path": "/etc/hermes/config.yaml"}, "task-001")

        mock_native.assert_called_once()
        mock_bridge.assert_not_called()
        assert result == native_result
        loop.close()

    def test_web_search_is_read(self) -> None:
        """web_search es READ — ejecuta nativo."""
        assert classify_nous_tool("web_search") is NousRisk.READ

    def test_search_files_is_read(self) -> None:
        """search_files es READ — ejecuta nativo."""
        assert classify_nous_tool("search_files") is NousRisk.READ

    def test_browser_snapshot_is_read(self) -> None:
        """browser_snapshot es READ (no modifica estado)."""
        assert classify_nous_tool("browser_snapshot") is NousRisk.READ

    def test_browser_navigate_is_write(self) -> None:
        """browser_navigate es WRITE (modifica sesión web)."""
        assert classify_nous_tool("browser_navigate") is NousRisk.WRITE

    def test_read_marks_taint_for_external_content(self) -> None:
        """web_search marca read_external_content=True (CTRL-5)."""
        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(engine_loop=loop)
        assert not agent._read_external_content

        with patch.object(agent, "_call_native_invoke", return_value='{"results": []}'):
            agent._invoke_tool("web_search", {"query": "test"}, "task-001")

        assert agent._read_external_content
        loop.close()

    def test_ha_get_state_read_does_not_taint(self) -> None:
        """ha_get_state es READ pero no es external content — no taint."""
        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(engine_loop=loop)

        with patch.object(agent, "_call_native_invoke", return_value='{"state": "on"}'):
            agent._invoke_tool("ha_get_state", {}, "task-001")

        assert not agent._read_external_content
        loop.close()


# ---------------------------------------------------------------------------
# (d) Outcomes: EXECUTED/REJECTED/PENDING
# ---------------------------------------------------------------------------


class TestOutcomeMapping:
    """Los tests de esta clase parchean _dispatch_via_bridge para controlar el outcome.

    Todos los agentes se construyen CON broker + consent + loop para que el
    guard "broker not wired" no se dispare antes de llegar al bridge.
    """

    def _wired_agent(self) -> tuple[GovernedAIAgent, asyncio.AbstractEventLoop]:
        """Agente completamente cableado (broker mock, consent, loop)."""
        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(
            broker=MagicMock(), consent_ctx=_consent_ctx(), engine_loop=loop
        )
        return agent, loop

    def test_executed_ok_returns_result_json(self) -> None:
        """EXECUTED → devuelve el resultado real del broker como JSON."""
        outcome = _outcome(ExecutionStatus.EXECUTED, result={"created": True, "id": "abc"})
        agent, loop = self._wired_agent()

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", return_value=outcome):
            result_str = agent._invoke_tool("write_file", {}, "task-001")

        parsed = json.loads(result_str)
        assert parsed["created"] is True
        assert parsed["id"] == "abc"
        loop.close()

    def test_rejected_by_policy_returns_blocked(self) -> None:
        """REJECTED_BY_POLICY → resultado contiene BLOCKED con razón."""
        outcome = _outcome(ExecutionStatus.REJECTED_BY_POLICY, error="denylist hit")
        agent, loop = self._wired_agent()

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", return_value=outcome):
            result_str = agent._invoke_tool("terminal", {"command": ["ls"]}, "task-001")

        parsed = json.loads(result_str)
        assert "BLOCKED" in parsed["error"]
        loop.close()

    def test_rejected_by_consent_returns_blocked(self) -> None:
        """REJECTED_BY_CONSENT → resultado contiene BLOCKED."""
        outcome = _outcome(ExecutionStatus.REJECTED_BY_CONSENT, error="operator_id ausente")
        agent, loop = self._wired_agent()

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", return_value=outcome):
            result_str = agent._invoke_tool("write_file", {}, "task-001")

        parsed = json.loads(result_str)
        assert "BLOCKED" in parsed["error"]
        loop.close()

    def test_pending_approval_returns_blocked_and_accumulates(self) -> None:
        """PENDING_APPROVAL → BLOCKED en result + proposal acumulada en _pending_proposals."""
        outcome = _outcome(ExecutionStatus.PENDING_APPROVAL)
        agent, loop = self._wired_agent()

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", return_value=outcome):
            result_str = agent._invoke_tool("skill_manage", {"action": "create"}, "task-001")

        parsed = json.loads(result_str)
        assert "BLOCKED" in parsed["error"]
        assert len(agent._pending_proposals) == 1
        assert agent._pending_proposals[0].tool_name == "skill_manage"
        loop.close()

    def test_multiple_pending_proposals_accumulated(self) -> None:
        """Varias calls PENDING_APPROVAL acumulan proposals en orden."""
        outcome = _outcome(ExecutionStatus.PENDING_APPROVAL)
        agent, loop = self._wired_agent()

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", return_value=outcome):
            agent._invoke_tool("write_file", {"path": "/a"}, "task-001")
            agent._invoke_tool("terminal", {"command": "ls"}, "task-001")

        assert len(agent._pending_proposals) == 2
        assert agent._pending_proposals[0].tool_name == "write_file"
        assert agent._pending_proposals[1].tool_name == "terminal"
        loop.close()

    def test_executed_proposals_not_accumulated(self) -> None:
        """EXECUTED proposals no se acumulan (ya resueltas in-loop)."""
        outcome = _outcome(ExecutionStatus.EXECUTED, result={"ok": True})
        agent, loop = self._wired_agent()

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", return_value=outcome):
            agent._invoke_tool("write_file", {}, "task-001")

        assert len(agent._pending_proposals) == 0
        loop.close()


# ---------------------------------------------------------------------------
# (e) Puente async: broker en otro event loop → resultado correcto
# ---------------------------------------------------------------------------


class TestAsyncBridge:
    def test_bridge_calls_broker_dispatch_from_sync_context(self) -> None:
        """_dispatch_via_bridge llama broker.dispatch desde un hilo y retorna el outcome."""
        expected_outcome = _outcome(ExecutionStatus.EXECUTED, result={"bridged": True})

        async def _fake_dispatch(proposal, consent_ctx, **kwargs):
            return expected_outcome

        # Simular: broker.dispatch es async y corre en el event loop principal.
        broker = MagicMock()
        broker.dispatch = AsyncMock(side_effect=_fake_dispatch)
        consent_ctx = _consent_ctx()
        proposal = _build_proposal(
            function_name="write_file",
            function_args={"path": "/x"},
            tenant_id=_TENANT,
            effective_task_id="task-bridge",
        )

        # Crear loop "principal" en un hilo de fondo (simula run_in_executor).
        results: list[Any] = []
        errors: list[Exception] = []

        def run_loop_in_bg(loop: asyncio.AbstractEventLoop) -> None:
            loop.run_forever()

        bg_loop = asyncio.new_event_loop()
        t = threading.Thread(target=run_loop_in_bg, args=(bg_loop,), daemon=True)
        t.start()

        try:
            outcome = _dispatch_via_bridge(
                proposal=proposal,
                broker=broker,
                consent_context=consent_ctx,
                engine_loop=bg_loop,
            )
            assert outcome.status is ExecutionStatus.EXECUTED
            assert outcome.result == {"bridged": True}
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=3)

    def test_bridge_timeout_returns_rejected_not_raises(self) -> None:
        """Timeout del bridge → REJECTED_BY_POLICY, no excepción al caller."""
        # Broker que nunca termina.
        async def _slow_dispatch(proposal, consent_ctx, **kwargs):
            await asyncio.sleep(9999)

        broker = MagicMock()
        broker.dispatch = AsyncMock(side_effect=_slow_dispatch)
        consent_ctx = _consent_ctx()
        proposal = _build_proposal(
            function_name="write_file",
            function_args={},
            tenant_id=_TENANT,
            effective_task_id="task-timeout",
        )

        bg_loop = asyncio.new_event_loop()
        t = threading.Thread(target=bg_loop.run_forever, daemon=True)
        t.start()

        try:
            # Parchear el timeout a 0.05s para el test.
            with patch("hermes.runtime.nous_engine._BROKER_DISPATCH_TIMEOUT_S", 0.05):
                outcome = _dispatch_via_bridge(
                    proposal=proposal,
                    broker=broker,
                    consent_context=consent_ctx,
                    engine_loop=bg_loop,
                )
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=3)

        assert outcome.status is ExecutionStatus.REJECTED_BY_POLICY
        assert "timeout" in (outcome.error or "").lower()

    def test_bridge_broker_exception_returns_rejected(self) -> None:
        """Excepción en broker.dispatch → REJECTED_BY_POLICY, no lanza al caller."""
        async def _failing_dispatch(proposal, consent_ctx, **kwargs):
            raise RuntimeError("broker internal error")

        broker = MagicMock()
        broker.dispatch = AsyncMock(side_effect=_failing_dispatch)
        consent_ctx = _consent_ctx()
        proposal = _build_proposal(
            function_name="write_file",
            function_args={},
            tenant_id=_TENANT,
            effective_task_id="task-err",
        )

        bg_loop = asyncio.new_event_loop()
        t = threading.Thread(target=bg_loop.run_forever, daemon=True)
        t.start()

        try:
            outcome = _dispatch_via_bridge(
                proposal=proposal,
                broker=broker,
                consent_context=consent_ctx,
                engine_loop=bg_loop,
            )
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=3)

        assert outcome.status is ExecutionStatus.REJECTED_BY_POLICY
        assert "broker_dispatch_error" in (outcome.error or "")

    def test_no_broker_returns_blocked_without_crash(self) -> None:
        """Sin broker configurado, WRITE devuelve BLOCKED — no lanza."""
        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(broker=None, engine_loop=None)

        result_str = agent._invoke_tool("write_file", {}, "task-001")

        parsed = json.loads(result_str)
        assert "BLOCKED" in parsed["error"]
        loop.close()


# ---------------------------------------------------------------------------
# (f) Cobertura del catálogo Nous — ninguna tool sin clasificar
# ---------------------------------------------------------------------------


class TestNousCatalogCoverage:
    """Falla si alguna tool del catálogo de Nous queda sin clasificar en el mapa."""

    def test_all_catalog_tools_are_classified(self) -> None:
        """Cada tool del NOUS_TOOL_CATALOG tiene una entrada en _NOUS_TOOL_RISK."""
        unclassified = [t for t in NOUS_TOOL_CATALOG if classify_nous_tool(t) is None]
        assert not unclassified, (
            f"Tools del catálogo de Nous sin clasificar (añadir a nous_tool_risk_map.py): "
            f"{sorted(unclassified)}"
        )

    def test_every_classified_tool_has_valid_risk(self) -> None:
        """Toda tool clasificada tiene NousRisk.READ o NousRisk.WRITE."""
        for tool in NOUS_TOOL_CATALOG:
            risk = classify_nous_tool(tool)
            assert risk in (NousRisk.READ, NousRisk.WRITE), (
                f"tool={tool!r} tiene riesgo inválido: {risk!r}"
            )

    def test_skill_manage_is_high_risk(self) -> None:
        """skill_manage es WRITE — F3 añade firma obligatoria."""
        assert classify_nous_tool("skill_manage") is NousRisk.WRITE

    def test_execute_code_is_high_risk(self) -> None:
        """execute_code es WRITE — ejecución arbitraria de código."""
        assert classify_nous_tool("execute_code") is NousRisk.WRITE

    def test_terminal_is_high_risk(self) -> None:
        """terminal es WRITE — shell arbitraria."""
        assert classify_nous_tool("terminal") is NousRisk.WRITE

    def test_computer_use_is_high_risk(self) -> None:
        """computer_use es WRITE — acceso a pantalla/teclado del SO."""
        assert classify_nous_tool("computer_use") is NousRisk.WRITE

    def test_unknown_tool_not_in_catalog(self) -> None:
        """Una tool no registrada por Nous devuelve None (fail-closed)."""
        assert classify_nous_tool("not_a_real_tool_xyz") is None

    def test_memory_tool_is_write(self) -> None:
        """memory modifica el MEMORY.md del agente — WRITE."""
        assert classify_nous_tool("memory") is NousRisk.WRITE

    def test_ha_call_service_is_write(self) -> None:
        """ha_call_service actúa sobre dispositivos físicos IoT — WRITE."""
        assert classify_nous_tool("ha_call_service") is NousRisk.WRITE

    def test_ha_get_state_is_read(self) -> None:
        """ha_get_state solo lee estado — READ."""
        assert classify_nous_tool("ha_get_state") is NousRisk.READ


# ---------------------------------------------------------------------------
# Extras: construcción de ToolCallProposal
# ---------------------------------------------------------------------------


class TestBuildProposal:
    def test_proposal_has_correct_tool_name(self) -> None:
        proposal = _build_proposal(
            function_name="write_file",
            function_args={"path": "/tmp/x"},
            tenant_id=_TENANT,
            effective_task_id="task-123",
        )
        assert proposal.tool_name == "write_file"
        assert proposal.tenant_id == _TENANT
        assert proposal.entity_id == "task-123"
        assert proposal.entity_type == "nous_tool"
        assert proposal.parameters == {"path": "/tmp/x"}

    def test_proposal_entity_id_fallback_when_task_empty(self) -> None:
        """Sin task_id, entity_id cae a 'nous_task' (válido, no empty)."""
        proposal = _build_proposal(
            function_name="terminal",
            function_args={},
            tenant_id=_TENANT,
            effective_task_id="",
        )
        assert proposal.entity_id == "nous_task"

    def test_proposal_is_immutable(self) -> None:
        """ToolCallProposal es frozen dataclass."""
        proposal = _build_proposal(
            function_name="write_file",
            function_args={},
            tenant_id=_TENANT,
            effective_task_id="task-x",
        )
        with pytest.raises((AttributeError, TypeError)):
            proposal.tool_name = "hacked"  # type: ignore[misc]

    def test_unique_proposal_ids(self) -> None:
        """Cada llamada genera un proposal_id distinto."""
        p1 = _build_proposal(function_name="write_file", function_args={}, tenant_id=_TENANT, effective_task_id="t")
        p2 = _build_proposal(function_name="write_file", function_args={}, tenant_id=_TENANT, effective_task_id="t")
        assert p1.proposal_id != p2.proposal_id


# ---------------------------------------------------------------------------
# Extras: taint de procedencia (CTRL-5)
# ---------------------------------------------------------------------------


class TestExternalContentTaint:
    def test_web_search_is_external(self) -> None:
        assert _is_external_content_tool("web_search") is True

    def test_web_extract_is_external(self) -> None:
        assert _is_external_content_tool("web_extract") is True

    def test_read_file_is_external(self) -> None:
        # Nous no tiene el allowlist de Hermes → conservador: untrusted.
        assert _is_external_content_tool("read_file") is True

    def test_browser_snapshot_is_external(self) -> None:
        assert _is_external_content_tool("browser_snapshot") is True

    def test_ha_get_state_not_external(self) -> None:
        assert _is_external_content_tool("ha_get_state") is False

    def test_kanban_list_not_external(self) -> None:
        assert _is_external_content_tool("kanban_list") is False
