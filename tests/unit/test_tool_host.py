from __future__ import annotations

import json
from uuid import UUID

import pytest

from hermes import ToolRisk, ToolSpec
from hermes.runtime.tool_host import CapturingToolHost

_TENANT = UUID("00000000-0000-0000-0000-000000000001")


async def _libros_handler(args: dict[str, object]) -> dict[str, object]:
    return {"libro_iva": "...", "args": args}


@pytest.fixture
def read_spec() -> ToolSpec:
    return ToolSpec(
        name="get_libros",
        description="Lee libros IVA",
        parameters_schema={"type": "object"},
        risk=ToolRisk.READ_ONLY,
        entity_type="libro",
        handler=_libros_handler,
    )


@pytest.fixture
def write_spec() -> ToolSpec:
    return ToolSpec(
        name="presentar_303",
        description="Presenta 303 definitivo",
        parameters_schema={"type": "object"},
        risk=ToolRisk.EXTERNA_IRREVERSIBLE,
        entity_type="cliente",
    )


def _make_call(call_id: str, name: str, args: dict[str, object]) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


@pytest.mark.asyncio
async def test_read_tool_is_executed(read_spec: ToolSpec) -> None:
    host = CapturingToolHost(specs=(read_spec,), tenant_id=_TENANT)
    round_result = await host.process_round(
        [_make_call("c1", "get_libros", {"cliente_nif": "12345678Z"})]
    )
    assert round_result.proposals == ()
    assert len(round_result.tool_results) == 1
    assert round_result.tool_results[0].name == "get_libros"
    assert "libro_iva" in round_result.tool_results[0].content


@pytest.mark.asyncio
async def test_write_tool_is_captured_not_executed(write_spec: ToolSpec) -> None:
    host = CapturingToolHost(specs=(write_spec,), tenant_id=_TENANT)
    round_result = await host.process_round(
        [
            _make_call(
                "c2",
                "presentar_303",
                {
                    "entity_id": "12345678Z",
                    "entity_type": "cliente",
                    "trimestre": "2T2026",
                    "justification": "Pendiente OK titular",
                },
            )
        ]
    )
    assert round_result.tool_results == ()
    assert len(round_result.proposals) == 1
    proposal = round_result.proposals[0]
    assert proposal.tool_name == "presentar_303"
    assert proposal.entity_id == "12345678Z"
    assert proposal.entity_type == "cliente"
    assert proposal.tenant_id == _TENANT
    assert "Pendiente OK titular" in proposal.justification


@pytest.mark.asyncio
async def test_unknown_tool_is_malformed(read_spec: ToolSpec) -> None:
    host = CapturingToolHost(specs=(read_spec,), tenant_id=_TENANT)
    round_result = await host.process_round(
        [_make_call("c3", "tool_inexistente", {})]
    )
    assert round_result.proposals == ()
    assert round_result.tool_results == ()
    assert len(round_result.malformed) == 1
    assert round_result.malformed[0]["reason"] == "unknown_tool"


@pytest.mark.asyncio
async def test_write_without_entity_id_is_malformed(write_spec: ToolSpec) -> None:
    host = CapturingToolHost(specs=(write_spec,), tenant_id=_TENANT)
    round_result = await host.process_round(
        [_make_call("c4", "presentar_303", {"trimestre": "2T2026"})]
    )
    assert round_result.proposals == ()
    assert len(round_result.malformed) == 1
    assert round_result.malformed[0]["reason"] == "missing_entity_id"


@pytest.mark.asyncio
async def test_duplicate_specs_rejected() -> None:
    spec = ToolSpec(
        name="x",
        description="x",
        parameters_schema={"type": "object"},
        risk=ToolRisk.READ_ONLY,
        handler=_libros_handler,
    )
    with pytest.raises(ValueError, match="duplicate tool name"):
        CapturingToolHost(specs=(spec, spec), tenant_id=_TENANT)


@pytest.mark.asyncio
async def test_openai_function_specs(read_spec: ToolSpec, write_spec: ToolSpec) -> None:
    host = CapturingToolHost(specs=(read_spec, write_spec), tenant_id=_TENANT)
    fns = host.openai_function_specs
    assert len(fns) == 2
    names = {fn["function"]["name"] for fn in fns}
    assert names == {"get_libros", "presentar_303"}
