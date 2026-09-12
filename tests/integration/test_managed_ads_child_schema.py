"""Explicit Ads source required; do not substitute a locally invented contract."""

import json
from importlib.resources import files

import pytest

pytest.importorskip("safent_ads", reason="Explicit companion source snapshot required")

from safent_ads.composition.managed_service import TOOL_MODELS, ManagedAdChildArgs  # noqa: E402

pytestmark = pytest.mark.integration


def test_managed_child_discovery_matches_companion_authoritative_schema():
    schemas = json.loads(
        files("hermes.runtime").joinpath("managed_ads_tool_schemas.json").read_text()
    )
    assert TOOL_MODELS["propose_ad_child"] is ManagedAdChildArgs
    assert schemas["propose_ad_child"] == ManagedAdChildArgs.model_json_schema()


def test_exact_scoped_paused_ad_roundtrips_companion_validator():
    business = "00000000-0000-4000-8000-000000000001"
    connection = "00000000-0000-4000-8000-000000000002"
    raw = {
        "entity_ref": f"meta:ad_set:{business}:{connection}:123/456",
        "child_plan": {
            "schema_version": 1,
            "platform": "meta",
            "kind": "ad",
            "status": "PAUSED",
            "native": {"name": "Anuncio propuesto", "creative_id": "789"},
        },
        "cause": {"text": "Propuesta para revisión humana"},
    }
    parsed = ManagedAdChildArgs.model_validate(raw)
    assert parsed.model_dump(mode="json", exclude_unset=True) == raw
    with pytest.raises(ValueError):
        ManagedAdChildArgs.model_validate(
            {
                **raw,
                "child_plan": {**raw["child_plan"], "status": "ACTIVE"},
            }
        )
    with pytest.raises(ValueError):
        ManagedAdChildArgs.model_validate({**raw, "business_id": business})
