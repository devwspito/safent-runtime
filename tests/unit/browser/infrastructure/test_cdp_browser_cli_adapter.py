"""Unit tests for CdpBrowserCliAdapter — semantic snapshot and render helpers.

No real browser required — Playwright objects are mocked via AsyncMock/MagicMock.

Coverage:
  - _render_a11y_node: flat node, nested node, empty role skipped,
    missing optional attrs omitted (used with synthetic Playwright-style trees).
  - _render_cdp_a11y_nodes: CDP flat node list rendering, ignored nodes skipped,
    interesting roles rendered, empty list → no lines.
  - CdpBrowserCliAdapter.snapshot(): a11y tree present, a11y tree empty,
    CDP session raises (fallback), body inner_text raises (fallback to content).
  - snapshot() format invariants: URL header, section headings, section order.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes.browser.infrastructure.cdp_browser_cli_adapter import (
    CdpBrowserCliAdapter,
    _render_a11y_node,
    _render_cdp_a11y_nodes,
)


# ---------------------------------------------------------------------------
# _render_a11y_node — pure function tests (Playwright-style tree dict)
# ---------------------------------------------------------------------------


class TestRenderA11yNode:
    def test_single_node_with_all_attrs(self) -> None:
        node = {"role": "button", "name": "Submit", "value": "submit", "description": "Sends form"}
        lines: list[str] = []
        _render_a11y_node(node, lines, depth=0)
        assert len(lines) == 1
        line = lines[0]
        assert "[button]" in line
        assert 'name="Submit"' in line
        assert 'value="submit"' in line
        assert 'description="Sends form"' in line

    def test_empty_role_node_skipped(self) -> None:
        node = {"role": "", "name": "Invisible"}
        lines: list[str] = []
        _render_a11y_node(node, lines, depth=0)
        assert lines == []

    def test_missing_role_node_skipped(self) -> None:
        node = {"name": "No role"}
        lines: list[str] = []
        _render_a11y_node(node, lines, depth=0)
        assert lines == []

    def test_empty_optional_attrs_omitted(self) -> None:
        node = {"role": "link", "name": "Click me", "value": "", "description": ""}
        lines: list[str] = []
        _render_a11y_node(node, lines, depth=0)
        assert len(lines) == 1
        assert 'value=""' not in lines[0]
        assert 'description=""' not in lines[0]
        assert 'name="Click me"' in lines[0]

    def test_nested_children_rendered_with_indent(self) -> None:
        node = {
            "role": "form",
            "name": "Login",
            "children": [
                {"role": "textbox", "name": "Username"},
                {"role": "button", "name": "Login"},
            ],
        }
        lines: list[str] = []
        _render_a11y_node(node, lines, depth=0)
        assert len(lines) == 3
        assert lines[0].startswith("[form]")
        assert lines[1].startswith("  [textbox]")
        assert lines[2].startswith("  [button]")

    def test_deeply_nested_indent(self) -> None:
        node = {
            "role": "region",
            "children": [
                {
                    "role": "list",
                    "children": [
                        {"role": "listitem", "name": "Item 1"},
                    ],
                }
            ],
        }
        lines: list[str] = []
        _render_a11y_node(node, lines, depth=0)
        assert lines[2].startswith("    [listitem]")  # depth 2 → 4 spaces

    def test_no_children_key_is_fine(self) -> None:
        node = {"role": "generic", "name": "Box"}
        lines: list[str] = []
        _render_a11y_node(node, lines, depth=0)
        assert len(lines) == 1

    def test_empty_children_list_is_fine(self) -> None:
        node = {"role": "generic", "name": "Box", "children": []}
        lines: list[str] = []
        _render_a11y_node(node, lines, depth=0)
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# _render_cdp_a11y_nodes — pure function tests (flat CDP node list)
# ---------------------------------------------------------------------------

def _cdp_node(role: str, name: str, *, ignored: bool = False) -> dict[str, Any]:
    """Helper: build a minimal CDP AX node dict."""
    return {
        "nodeId": "1",
        "ignored": ignored,
        "role": {"value": role},
        "name": {"value": name},
    }


class TestRenderCdpA11yNodes:
    def test_button_node_rendered(self) -> None:
        nodes = [_cdp_node("button", "Submit order")]
        lines: list[str] = []
        _render_cdp_a11y_nodes(nodes, lines)
        assert len(lines) == 1
        assert "[button]" in lines[0]
        assert 'name="Submit order"' in lines[0]

    def test_textbox_node_rendered(self) -> None:
        nodes = [_cdp_node("textbox", "Customer name")]
        lines: list[str] = []
        _render_cdp_a11y_nodes(nodes, lines)
        assert "[textbox]" in lines[0]
        assert 'name="Customer name"' in lines[0]

    def test_ignored_nodes_skipped(self) -> None:
        nodes = [_cdp_node("button", "Hidden", ignored=True)]
        lines: list[str] = []
        _render_cdp_a11y_nodes(nodes, lines)
        assert lines == []

    def test_none_role_skipped(self) -> None:
        nodes = [_cdp_node("none", "")]
        lines: list[str] = []
        _render_cdp_a11y_nodes(nodes, lines)
        assert lines == []

    def test_empty_node_list(self) -> None:
        lines: list[str] = []
        _render_cdp_a11y_nodes([], lines)
        assert lines == []

    def test_multiple_nodes_all_rendered(self) -> None:
        nodes = [
            _cdp_node("textbox", "Username"),
            _cdp_node("textbox", "Password"),
            _cdp_node("button", "Login"),
        ]
        lines: list[str] = []
        _render_cdp_a11y_nodes(nodes, lines)
        assert len(lines) == 3

    def test_nameless_uninteresting_role_skipped(self) -> None:
        """Nodes with no name and an unrecognized role are skipped."""
        nodes = [{"nodeId": "1", "ignored": False, "role": {"value": "generic"}, "name": {"value": ""}}]
        lines: list[str] = []
        _render_cdp_a11y_nodes(nodes, lines)
        assert lines == [], "generic role with empty name should be skipped"

    def test_nameless_interesting_role_included(self) -> None:
        """Nodes with a recognized role but no name are still included."""
        nodes = [{"nodeId": "1", "ignored": False, "role": {"value": "RootWebArea"}, "name": {"value": ""}}]
        lines: list[str] = []
        _render_cdp_a11y_nodes(nodes, lines)
        assert len(lines) == 1
        assert "[RootWebArea]" in lines[0]


# ---------------------------------------------------------------------------
# CdpBrowserCliAdapter.snapshot() — unit tests with mocked driver
# ---------------------------------------------------------------------------


def _make_adapter_with_mock_page(
    *,
    cdp_nodes: list[dict[str, Any]] | None = None,
    cdp_raises: Exception | None = None,
    inner_text: str = "page body text",
    inner_text_raises: Exception | None = None,
    content: str = "<html>fallback</html>",
    url: str = "https://example.com/",
) -> tuple[CdpBrowserCliAdapter, MagicMock]:
    """Build a CdpBrowserCliAdapter with a fully mocked Playwright page.

    Mocks the CDP session path (context.new_cdp_session + send) rather than
    the removed page.accessibility API.
    """
    mock_page = MagicMock()
    mock_page.url = url

    # CDP session mock
    mock_cdp_session = MagicMock()
    if cdp_raises:
        mock_cdp_session.send = AsyncMock(side_effect=cdp_raises)
    else:
        nodes = cdp_nodes if cdp_nodes is not None else []
        mock_cdp_session.send = AsyncMock(return_value={"nodes": nodes})
    mock_cdp_session.detach = AsyncMock()

    mock_context = MagicMock()
    mock_context.new_cdp_session = AsyncMock(return_value=mock_cdp_session)
    mock_page.context = mock_context

    # Body locator mock
    mock_locator = MagicMock()
    if inner_text_raises:
        mock_locator.inner_text = AsyncMock(side_effect=inner_text_raises)
    else:
        mock_locator.inner_text = AsyncMock(return_value=inner_text)
    mock_page.locator = MagicMock(return_value=mock_locator)
    mock_page.content = AsyncMock(return_value=content)

    mock_driver = MagicMock()
    mock_driver._page = mock_page
    mock_driver._cdp_url = "http://127.0.0.1:9222"

    adapter = CdpBrowserCliAdapter(driver=mock_driver)
    adapter._started = True  # skip start()

    return adapter, mock_page


@pytest.mark.asyncio
async def test_snapshot_contains_url_header() -> None:
    adapter, _ = _make_adapter_with_mock_page(url="https://httpbin.org/forms/post")
    result = await adapter.snapshot()
    assert result.startswith("URL: https://httpbin.org/forms/post")


@pytest.mark.asyncio
async def test_snapshot_contains_a11y_section_heading() -> None:
    adapter, _ = _make_adapter_with_mock_page()
    result = await adapter.snapshot()
    assert "=== Accessibility Tree ===" in result


@pytest.mark.asyncio
async def test_snapshot_contains_page_text_section_heading() -> None:
    adapter, _ = _make_adapter_with_mock_page()
    result = await adapter.snapshot()
    assert "=== Page Text ===" in result


@pytest.mark.asyncio
async def test_snapshot_renders_a11y_roles_and_names() -> None:
    """When CDP returns nodes, they appear in the snapshot with roles and names."""
    nodes = [
        {"nodeId": "1", "ignored": False, "role": {"value": "textbox"}, "name": {"value": "Customer name"}},
        {"nodeId": "2", "ignored": False, "role": {"value": "button"}, "name": {"value": "Submit order"}},
    ]
    adapter, _ = _make_adapter_with_mock_page(cdp_nodes=nodes)
    result = await adapter.snapshot()
    assert "[textbox]" in result
    assert 'name="Customer name"' in result
    assert "[button]" in result
    assert 'name="Submit order"' in result


@pytest.mark.asyncio
async def test_snapshot_with_empty_cdp_node_list() -> None:
    """When CDP returns zero nodes, emit the empty-tree message."""
    adapter, _ = _make_adapter_with_mock_page(cdp_nodes=[])
    result = await adapter.snapshot()
    assert "empty accessibility tree" in result


@pytest.mark.asyncio
async def test_snapshot_cdp_exception_falls_back_gracefully() -> None:
    """When CDP send() raises, emit the unavailable message (no crash)."""
    adapter, _ = _make_adapter_with_mock_page(
        cdp_raises=RuntimeError("CDP protocol error")
    )
    result = await adapter.snapshot()
    assert "accessibility tree unavailable" in result
    # Page text section should still be present
    assert "=== Page Text ===" in result


@pytest.mark.asyncio
async def test_snapshot_body_inner_text_exception_falls_back_to_content() -> None:
    """When inner_text raises, snapshot() falls back to page.content()."""
    adapter, _ = _make_adapter_with_mock_page(
        inner_text_raises=TimeoutError("locator timeout"),
        content="<html><body>fallback content</body></html>",
    )
    result = await adapter.snapshot()
    assert "fallback content" in result


@pytest.mark.asyncio
async def test_snapshot_page_text_is_present_in_output() -> None:
    adapter, _ = _make_adapter_with_mock_page(inner_text="visible body text here")
    result = await adapter.snapshot()
    assert "visible body text here" in result


@pytest.mark.asyncio
async def test_snapshot_section_order() -> None:
    """URL comes first, then a11y tree, then page text."""
    nodes = [
        {"nodeId": "1", "ignored": False, "role": {"value": "button"}, "name": {"value": "Go"}},
    ]
    adapter, _ = _make_adapter_with_mock_page(
        cdp_nodes=nodes,
        inner_text="body",
        url="https://example.com/",
    )
    result = await adapter.snapshot()
    url_idx = result.index("URL:")
    a11y_idx = result.index("=== Accessibility Tree ===")
    text_idx = result.index("=== Page Text ===")
    assert url_idx < a11y_idx < text_idx, (
        f"Expected URL < a11y < text; got positions {url_idx}, {a11y_idx}, {text_idx}"
    )
