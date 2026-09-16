"""spec 022 v2 — tailnet_ssh / tailnet_file_get / tailnet_file_put LLM wiring.

Before this, the governed use cases (TailnetSshUseCase, TailnetFileGetUseCase,
TailnetFilePutUseCase) and the Step 1.6-tailnet_ssh gate existed, but NOTHING
put these three tool names into the LLM-visible schema or dispatched a call
to the use cases — see specs/022-tailnet-connectivity/ssh-v2.md §Deferred
"Live LLM-tool-call wiring". This locks the wiring:

  1. The three tools appear in build_capability_tool_specs' output, with the
     exact JSON schemas from ssh-v2.md/contracts.md (host/command/timeout_s/
     stdin for tailnet_ssh; host/path for file_get; host/path/content for
     file_put).
  2. risk=LOW ⇒ READ_ONLY ⇒ a broker-dispatching handler (never handler=None
     — that would route as a WRITE proposal through the broker's OWN
     risk-based HITL, the "second, conflicting approval surface" the spec
     explicitly rejected as the tailnet_ssh gate).
  3. classify_nous_tool() returns None for all three — they are NOT native
     Nous tools, so _resolve_external_specs (nous_engine.py) never filters
     them out. Regression test: they must NEVER be added to
     runtime/nous_tool_risk_map.py (that IS the native-catalog filter; an
     entry there would silently delete the ToolSpec _resolve_external_specs
     emits to the LLM — see tool_delicacy.py's own comment on why
     tailnet_ssh is hand-listed in _DELICATE_NON_NATIVE instead).
  4. The handler reaches broker.dispatch exactly once, with `op` injected
     to the tool_name (mirrors the BROWSER convention) — never a direct
     adapter/use-case call from the tool-spec layer (broker is the single
     choke-point).
  5. advertise ⟺ executable: the tool is present when SurfaceKind.TAILNET_SSH
     is in registered_surface_kinds, absent when it is not.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.domain.ports import ConsentContext, ExecutionOutcome, ExecutionStatus
from hermes.domain.tool_spec import ToolRisk
from hermes.runtime.capability_tool_specs import _TOOL_SCHEMAS, build_capability_tool_specs
from hermes.runtime.nous_tool_risk_map import classify_nous_tool

pytestmark = pytest.mark.unit

_TENANT = UUID("30000000-0000-0000-0000-000000000001")
_OPERATOR = UUID("30000000-0000-0000-0000-000000000002")
_TAILNET_TOOL_NAMES = ("tailnet_ssh", "tailnet_file_get", "tailnet_file_put")


def _consent_ctx() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)


def _mock_broker(status: ExecutionStatus = ExecutionStatus.EXECUTED, result: dict | None = None):
    broker = MagicMock()
    broker.dispatch = AsyncMock(
        return_value=ExecutionOutcome(
            proposal_id=uuid4(), status=status, result=result or {},
        )
    )
    return broker


def _build_specs(broker: Any = None, registered_surface_kinds: frozenset | None = None):
    specs, _ref = build_capability_tool_specs(
        broker=broker or _mock_broker(),
        consent_context=_consent_ctx(),
        registered_surface_kinds=registered_surface_kinds,
    )
    return specs


class TestSchemaPresence:
    def test_all_three_tools_have_schemas(self) -> None:
        for name in _TAILNET_TOOL_NAMES:
            assert name in _TOOL_SCHEMAS, f"{name} missing from _TOOL_SCHEMAS"

    def test_tailnet_ssh_schema_matches_contract(self) -> None:
        schema = _TOOL_SCHEMAS["tailnet_ssh"]
        props = schema["properties"]
        assert set(schema["required"]) == {"host", "command"}
        assert props["host"]["type"] == "string"
        assert props["command"]["type"] == "string"
        assert props["timeout_s"]["minimum"] == 1
        assert props["timeout_s"]["maximum"] == 300
        assert props["stdin"]["type"] == "string"

    def test_tailnet_file_get_schema_matches_contract(self) -> None:
        schema = _TOOL_SCHEMAS["tailnet_file_get"]
        assert set(schema["required"]) == {"host", "path"}

    def test_tailnet_file_put_schema_matches_contract(self) -> None:
        schema = _TOOL_SCHEMAS["tailnet_file_put"]
        assert set(schema["required"]) == {"host", "path", "content"}


class TestBuildCapabilityToolSpecsIncludesTailnetSsh:
    def test_all_three_tools_present(self) -> None:
        names = {s.name for s in _build_specs()}
        for name in _TAILNET_TOOL_NAMES:
            assert name in names, f"{name} missing from build_capability_tool_specs output"

    def test_all_three_are_read_only_with_handler(self) -> None:
        specs = {s.name: s for s in _build_specs()}
        for name in _TAILNET_TOOL_NAMES:
            spec = specs[name]
            assert spec.risk is ToolRisk.READ_ONLY, (
                f"{name} must be READ_ONLY — HIGH would trigger the broker's own "
                "risk-based HITL, a second approval surface on top of Step "
                "1.6-tailnet_ssh (see specs/022-tailnet-connectivity/ssh-v2.md "
                "§Rejected alternative)."
            )
            assert spec.handler is not None, f"{name} must have a broker-dispatching handler"

    def test_entity_type_is_os_surface(self) -> None:
        specs = {s.name: s for s in _build_specs()}
        for name in _TAILNET_TOOL_NAMES:
            assert specs[name].entity_type == "os_surface"


class TestNotInNativeNousCatalog:
    """Regression: an entry in nous_tool_risk_map.py would silently remove
    these tools from _resolve_external_specs' output (nous_engine.py:2585,
    `classify_nous_tool(s.name) is None`)."""

    def test_classify_nous_tool_returns_none_for_all_three(self) -> None:
        for name in _TAILNET_TOOL_NAMES:
            assert classify_nous_tool(name) is None, (
                f"{name} must NOT be classified in nous_tool_risk_map.py — doing so "
                "would filter its ToolSpec out of _resolve_external_specs and the "
                "LLM would never see it again."
            )


class TestOpInjectionAndBrokerDispatch:
    @pytest.mark.parametrize("tool_name", _TAILNET_TOOL_NAMES)
    async def test_handler_reaches_broker_exactly_once_with_op_injected(
        self, tool_name: str
    ) -> None:
        dispatch_calls: list[Any] = []

        broker = MagicMock()

        async def _dispatch(proposal, ctx, **kwargs):  # noqa: ARG001
            dispatch_calls.append(proposal)
            return ExecutionOutcome(proposal_id=proposal.proposal_id, status=ExecutionStatus.EXECUTED)

        broker.dispatch = _dispatch

        specs, _ref = build_capability_tool_specs(broker=broker, consent_context=_consent_ctx())
        spec = next(s for s in specs if s.name == tool_name)

        await spec.handler({"host": "db1", "command": "ls"} if tool_name == "tailnet_ssh" else {"host": "db1", "path": "/etc/hostname"})

        assert len(dispatch_calls) == 1, f"broker.dispatch called {len(dispatch_calls)} times for {tool_name}"
        assert dispatch_calls[0].tool_name == tool_name
        assert dispatch_calls[0].parameters.get("op") == tool_name

    async def test_blocked_dispatch_returns_error_dict_not_exception(self) -> None:
        broker = _mock_broker(status=ExecutionStatus.REJECTED_BY_POLICY)
        specs, _ref = build_capability_tool_specs(broker=broker, consent_context=_consent_ctx())
        spec = next(s for s in specs if s.name == "tailnet_ssh")

        result = await spec.handler({"host": "db1", "command": "ls"})

        assert "error" in result


class TestAdvertiseEqualsExecutable:
    def test_present_when_surface_kind_registered(self) -> None:
        names = {
            s.name
            for s in _build_specs(registered_surface_kinds=frozenset({SurfaceKind.TAILNET_SSH}))
        }
        assert "tailnet_ssh" in names

    def test_absent_when_surface_kind_not_registered(self) -> None:
        names = {
            s.name
            for s in _build_specs(registered_surface_kinds=frozenset({SurfaceKind.BROWSER}))
        }
        assert "tailnet_ssh" not in names
        assert "tailnet_file_get" not in names
        assert "tailnet_file_put" not in names
