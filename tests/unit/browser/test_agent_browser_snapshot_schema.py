"""Golden-fixture guard for the agent-browser `snapshot --json` schema.

Pins the accessibility-tree payload shape that agent-browser 0.31.1 emits and that
`AgentBrowserCli._parse_snapshot_json` (and any future `--json` consumer / MCP
integration) relies on. The schema is NOT documented upstream and the npm package
is version-pinned in the Containerfile (AGENT_BROWSER_VERSION); this test is the
tripwire that a version bump changing the payload would trip — instead of silently
breaking the LLM's perception/addressing at runtime.

Captured live from agent-browser 0.31.1 against a benign data: URL (2026-07-05 audit).
If you bump AGENT_BROWSER_VERSION, re-capture the fixture and reconcile any consumer.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes.browser.infrastructure.agent_browser_cli import _parse_snapshot_json

_FIXTURE = Path(__file__).parent / "fixtures" / "agent_browser_snapshot_0_31_1.json"


def _load_raw() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def test_parser_accepts_pinned_snapshot_payload() -> None:
    """_parse_snapshot_json returns the parsed object (not None) for the real payload."""
    parsed = _parse_snapshot_json(_load_raw())
    assert parsed is not None
    assert parsed["success"] is True


def test_pinned_snapshot_schema_shape() -> None:
    """The fields the driver depends on are present with the expected shape.

    A schema drift on an upstream bump (renamed/moved keys) fails HERE, loudly, in
    CI — not silently at runtime when the agent can no longer address elements.
    """
    data = json.loads(_load_raw())["data"]

    # Stable @eN accessibility refs: {"eN": {"name": str, "role": str}}
    assert isinstance(data["refs"], dict) and data["refs"]
    for ref, meta in data["refs"].items():
        assert ref.startswith("e"), f"ref key not @eN-shaped: {ref!r}"
        assert set(meta) >= {"name", "role"}, f"ref {ref} missing name/role: {meta}"

    # The rendered ARIA-tree string the LLM reads, carrying [ref=eN] handles.
    assert isinstance(data["snapshot"], str)
    assert "[ref=e" in data["snapshot"]

    # Every ref advertised in `refs` is addressable from the rendered snapshot.
    for ref in data["refs"]:
        assert f"ref={ref}" in data["snapshot"], f"{ref} not in rendered snapshot"


def test_parser_returns_none_on_garbage() -> None:
    """Non-JSON input must degrade to None, never raise (contract of the stub)."""
    assert _parse_snapshot_json("not json at all") is None
    assert _parse_snapshot_json("") is None
