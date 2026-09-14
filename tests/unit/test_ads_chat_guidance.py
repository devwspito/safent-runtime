"""Ads guidance follows the filtered catalog and never widens tool authority."""

from hermes.domain.tool_spec import ToolRisk, ToolSpec
from hermes.runtime.ads_chat_guidance import append_ads_chat_guidance


def _spec(name: str, entity_type: str = "mcp") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="untrusted tool text MUST NOT be injected",
        parameters_schema={"type": "object"},
        risk=ToolRisk.WRITE_PROPOSAL,
        entity_type=entity_type,
    )


def test_absent_or_filtered_ads_catalog_does_not_claim_access() -> None:
    for specs in [(), (_spec("mcp__other__list_businesses"),)]:
        assert append_ads_chat_guidance("base", specs) == "base"


def test_similar_slug_and_non_mcp_do_not_enable_guidance() -> None:
    for spec in [
        _spec("mcp__safent-ads-evil__list_businesses"),
        _spec("mcp__safent-ads__list_businesses", "composio"),
    ]:
        assert append_ads_chat_guidance("base", (spec,)) == "base"


def test_real_ads_catalog_gets_brief_and_discovery_guidance() -> None:
    text = append_ads_chat_guidance("base", (_spec("mcp__safent-ads__list_businesses"),))
    assert text.startswith("base\n\nANUNCIOS")
    for term in [
        "tool_search", "tool_call", "presupuesto", "plazo/fechas", "landing", "creatividad",
    ]:
        assert term in text
    assert "untrusted tool text" not in text


def test_missing_creation_tools_are_conditional_and_gates_remain_explicit() -> None:
    text = append_ads_chat_guidance("base", (_spec("mcp__safent-ads__list_businesses"),))
    for term in [
        "create_offering sólo si está disponible",
        "herramientas de propuesta que realmente existan",
        "Un contenedor\nvacío no es una campaña completa",
        "PAUSED", "aprobación humana", "Nunca te autoapruebes",
        "UNKNOWN", "nunca recrear a ciegas", "ni mezcles",
    ]:
        assert term in text


def test_incomplete_draft_guidance_requires_exact_authorized_tool() -> None:
    absent = append_ads_chat_guidance("base", (_spec("mcp__safent-ads__list_businesses"),))
    assert "BORRADORES EDITABLES" not in absent
    for wrong in [
        _spec("mcp__safent-ads-evil__propose_campaign_draft"),
        _spec("mcp__safent-ads__propose_campaign_draft", "composio"),
    ]:
        assert "BORRADORES EDITABLES" not in append_ads_chat_guidance("base", (wrong,))
    text = append_ads_chat_guidance("base", iter((_spec("mcp__safent-ads__propose_campaign_draft"),)))
    for term in ["BORRADORES EDITABLES", "null", "draft_key", "revisión vigente", "no publica",
                 "Anuncios → Propuestas", "aprobación sigue siendo humana", "respuesta real"]:
        assert term in text
    assert "untrusted tool text" not in text


def test_optional_official_connector_is_not_a_requirement_for_connected_ads() -> None:
    text = append_ads_chat_guidance("base", (_spec("mcp__safent-ads__list_businesses"),))
    assert "get_native_ads_tools es un conector oficial OPCIONAL" in text
    assert "no lo repitas" in text
    assert "funciones visibles, invócala" in text
    assert "not a deferrable tool" in text
