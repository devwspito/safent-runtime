"""MCP argument schemas reach the model with nested models inlined.

Pydantic/FastMCP publish nested argument models through ``$defs`` + ``$ref``;
function-calling surfaces do not reliably render those, so the model saw
safent-ads ``propose_campaign_draft.changes`` as a bare object and guessed its
keys (nine "extra inputs are not permitted" errors, 2026-09-14).
"""
from __future__ import annotations

from hermes.runtime.mcp_tool_specs import inline_local_refs


def _draft_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "args": {"$ref": "#/$defs/DraftSaveArgs"},
        },
        "required": ["args"],
        "$defs": {
            "DraftSaveArgs": {
                "type": "object",
                "properties": {
                    "business_id": {"type": "string"},
                    "changes": {"$ref": "#/$defs/DraftFields", "description": "PATCH"},
                },
                "required": ["business_id", "changes"],
                "additionalProperties": False,
            },
            "DraftFields": {
                "type": "object",
                "properties": {
                    "platform": {"enum": ["google", "meta"]},
                    "daily_budget": {"anyOf": [{"$ref": "#/$defs/DraftBudget"}, {"type": "null"}]},
                },
                "additionalProperties": False,
            },
            "DraftBudget": {"type": "object", "properties": {"amount": {"type": "string"}}},
        },
    }


def test_nested_refs_are_inlined_and_defs_dropped() -> None:
    out = inline_local_refs(_draft_schema())

    args = out["properties"]["args"]
    assert "$ref" not in args and args["type"] == "object"
    changes = args["properties"]["changes"]
    assert changes["properties"]["platform"] == {"enum": ["google", "meta"]}
    assert changes["description"] == "PATCH", "sibling keys next to $ref survive"
    assert changes["additionalProperties"] is False
    budget = changes["properties"]["daily_budget"]["anyOf"][0]
    assert budget["properties"]["amount"] == {"type": "string"}
    assert "$defs" not in out and "$defs" not in args


def test_schema_without_defs_is_returned_untouched() -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    assert inline_local_refs(schema) is schema


def test_recursive_refs_stop_at_the_depth_limit_instead_of_looping() -> None:
    schema = {
        "type": "object",
        "properties": {"node": {"$ref": "#/$defs/Node"}},
        "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
    }
    out = inline_local_refs(schema)  # must terminate
    node = out["properties"]["node"]
    depth = 0
    while isinstance(node, dict) and "properties" in node:
        node = node["properties"]["child"]
        depth += 1
    assert depth >= 1 and "$ref" in node


def test_unknown_ref_is_left_as_is() -> None:
    schema = {"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}}, "$defs": {"Y": {}}}
    out = inline_local_refs(schema)
    assert out["properties"]["x"] == {"$ref": "#/$defs/Missing"}
