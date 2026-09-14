"""Ads guidance follows the registered catalog and never widens tool authority."""

from hermes.domain.tool_spec import ToolRisk, ToolSpec
from hermes.runtime.ads_chat_guidance import append_ads_chat_guidance
from hermes.runtime.conversation_task_registry import set_visible_external_names
from hermes.runtime.nous_engine import _apply_ads_chat_guidance, _visible_external_specs


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


def test_required_review_before_proposing_a_campaign() -> None:
    text = append_ads_chat_guidance("base", (_spec("mcp__safent-ads__list_businesses"),))
    for term in [
        "Revisión previa obligatoria",
        "search_competitor_ads",
        "list_top_performing_ads",
        "30-90 días",
        '"why"',
        "incompleta",
    ]:
        assert term in text


def test_reference_data_tools_avoid_asking_the_owner_for_known_ids() -> None:
    text = append_ads_chat_guidance("base", (_spec("mcp__safent-ads__list_businesses"),))
    for term in [
        "list_meta_pages", "list_meta_pixels", "list_meta_audiences",
        "list_google_conversion_actions", "search_google_constants",
        "get_google_keyword_ideas", "nunca pidas al usuario un ID",
    ]:
        assert term in text


def test_creative_tools_prefer_generated_assets_over_invention() -> None:
    text = append_ads_chat_guidance("base", (_spec("mcp__safent-ads__list_businesses"),))
    for term in [
        "upload_creative_asset", "generate_creative_assets", "FAL", "no inventes assets",
    ]:
        assert term in text


def test_optimisation_tool_set_is_complete_and_native_write_is_last_resort() -> None:
    text = append_ads_chat_guidance("base", (_spec("mcp__safent-ads__list_businesses"),))
    for term in [
        "propose_budget_change", "propose_pause", "propose_resume", "propose_bid_target",
        "propose_negative_keywords", "propose_targeting_change", "propose_creative_rotation",
        "propose_creative_publication", "propose_delete",
        "propose_native_write sólo si\nninguna existe",
    ]:
        assert term in text


def test_missing_creation_tools_are_conditional_and_gates_remain_explicit() -> None:
    text = append_ads_chat_guidance("base", (_spec("mcp__safent-ads__list_businesses"),))
    for term in [
        "create_offering sólo si está disponible",
        "herramientas de propuesta que realmente existan",
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
    draft_spec = _spec("mcp__safent-ads__propose_campaign_draft")
    text = append_ads_chat_guidance("base", iter((draft_spec,)))
    for term in [
        "BORRADORES EDITABLES", "creation_plan es opcional", "null", "draft_key",
        "revisión vigente", "no\npublica", "Anuncios → Propuestas",
        "aprobación sigue siendo\nhumana", "respuesta real",
    ]:
        assert term in text
    assert "untrusted tool text" not in text


def test_optional_official_connector_is_not_a_requirement_for_connected_ads() -> None:
    text = append_ads_chat_guidance("base", (_spec("mcp__safent-ads__list_businesses"),))
    assert "get_native_ads_tools\nes un conector oficial OPCIONAL" in text
    assert "no lo repitas" in text
    assert "funciones\nvisibles, invócalas directo" in text
    assert "not a deferrable tool" in text


class TestGuidanceKeysOffRegistrationNotThisTurnsVisibility:
    """Parity fix 2.5 regression: `_apply_ads_chat_guidance` (nous_engine's call
    site) must be fed the FULL registered catalog, never the per-turn visible
    subset — otherwise a turn where top-K narrowing hides the ads companion
    would silently drop the mandatory pre-campaign-review guidance.
    """

    def test_guidance_survives_a_turn_that_narrows_the_ads_tool_out_of_visibility(
        self,
    ) -> None:
        full_catalog = (
            _spec("mcp__safent-ads__list_businesses"),
            _spec("mcp__gmail__fetch"),
        )
        set_visible_external_names(frozenset({"mcp__gmail__fetch"}))
        try:
            visible_this_turn = _visible_external_specs(full_catalog)
            assert visible_this_turn == (full_catalog[1],), (
                "sanity: this turn's narrowing hides the ads tool"
            )

            text = _apply_ads_chat_guidance("base", "chat_message", full_catalog)
        finally:
            set_visible_external_names(None)

        assert text.startswith("base\n\nANUNCIOS")

    def test_non_chat_trigger_never_attaches_guidance(self) -> None:
        full_catalog = (_spec("mcp__safent-ads__list_businesses"),)

        assert _apply_ads_chat_guidance("base", "autonomous_cycle", full_catalog) == "base"
        assert _apply_ads_chat_guidance("base", None, full_catalog) == "base"
