from __future__ import annotations

import pytest

from hermes import ToolRisk, ToolSpec


async def _noop(_: dict[str, object]) -> dict[str, object]:
    return {}


def test_read_only_requires_handler() -> None:
    with pytest.raises(ValueError, match="READ_ONLY tools must provide a handler"):
        ToolSpec(
            name="get_libros",
            description="Lee libros IVA del cliente",
            parameters_schema={"type": "object"},
            risk=ToolRisk.READ_ONLY,
        )


def test_write_must_not_provide_handler() -> None:
    with pytest.raises(ValueError, match="only READ_ONLY tools may provide a handler"):
        ToolSpec(
            name="presentar_303",
            description="Presenta 303 definitivo",
            parameters_schema={"type": "object"},
            risk=ToolRisk.EXTERNA_IRREVERSIBLE,
            handler=_noop,
        )


def test_openai_function_serialization() -> None:
    spec = ToolSpec(
        name="pause_campaign",
        description="Pausa una campana",
        parameters_schema={
            "type": "object",
            "properties": {"campaign_id": {"type": "string"}},
            "required": ["campaign_id"],
        },
        risk=ToolRisk.WRITE_PROPOSAL,
        entity_type="campaign",
    )
    out = spec.to_openai_function()
    assert out["type"] == "function"
    assert out["function"]["name"] == "pause_campaign"
    assert out["function"]["parameters"]["required"] == ["campaign_id"]
    assert spec.is_write is True


def test_read_only_tool_is_not_write() -> None:
    spec = ToolSpec(
        name="get_libros",
        description="Lee libros IVA",
        parameters_schema={"type": "object"},
        risk=ToolRisk.READ_ONLY,
        handler=_noop,
    )
    assert spec.is_write is False
