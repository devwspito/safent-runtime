"""CdpBrowserCliAdapter: AgentBrowserCli-compatible adapter backed by CdpPlaywrightDriver.

Activates when HERMES_BROWSER_SANDBOX=openshell. Implements the same async
interface as AgentBrowserCli (navigate / snapshot / click / type_ / current_url
/ close) but delegates to a CdpPlaywrightDriver that attaches to the pre-existing
Chromium inside an OpenShell sandbox via CDP, rather than spawning a local browser
binary.

Design constraints (spec 012):
  - Drop-in replacement: BrowserSurfaceAdapter receives this object in the same
    field (session.cli) it normally holds an AgentBrowserCli — no changes to
    BrowserSurfaceAdapter are required.
  - Lifecycle: start() calls CdpPlaywrightDriver.start() once. close() delegates to
    driver.close(), which disconnects from the remote browser WITHOUT stopping
    the Chromium process inside the sandbox (the sandbox lifecycle is managed by
    OpenShellSandboxProvider, not by this adapter).
  - Snapshot: returns the full page text content since agent-browser's accessibility
    tree format is not available via raw CDP. Callers use snapshot text for READ_ONLY
    observation; format difference is acceptable for this surface.
  - No subprocess spawning — subprocess.run/create_subprocess_exec are NOT called.
    All I/O goes through Playwright async API.
  - Thread safety: single-coroutine use assumed (same as AgentBrowserCli).

Placement: infrastructure layer. No domain types imported.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hermes.browser.infrastructure.cdp_playwright_driver import CdpPlaywrightDriver

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_MS: int = 20_000


class CdpBrowserCliAdapter:
    """AgentBrowserCli-compatible adapter over CdpPlaywrightDriver.

    Construction:
        adapter = CdpBrowserCliAdapter(driver=CdpPlaywrightDriver(cdp_url=...))
        await adapter.start()
        # use navigate / snapshot / click / type_ / current_url
        await adapter.close()

    The driver is started lazily on the first operation or explicitly via start().
    close() delegates to driver.close() — does not stop the remote Chromium.
    """

    def __init__(
        self,
        *,
        driver: "CdpPlaywrightDriver",
        timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    ) -> None:
        self._driver = driver
        self._timeout_ms = timeout_ms
        self._started = False

    async def start(self) -> None:
        """Connect to the remote Chromium via CDP."""
        if self._started:
            return
        await self._driver.start()
        self._started = True
        logger.info(
            "hermes.browser.cdp_cli_adapter.started",
            extra={"cdp_url": self._driver._cdp_url},
        )

    async def navigate(self, url: str) -> None:
        """Navigate the sandbox browser to url."""
        await self._ensure_started()
        page = self._driver._page
        await page.goto(url, timeout=self._timeout_ms)
        logger.debug(
            "hermes.browser.cdp_cli_adapter.navigated",
            extra={"url": page.url},
        )

    async def snapshot(self) -> str:
        """Return a semantic snapshot of the current page.

        Combines two sources so the agent has both structure and raw text:
          1. Accessibility tree (CDP Accessibility.getFullAXTree) — roles, names,
             descriptions, and values that let the agent identify interactive
             elements without relying on fragile CSS selectors.
          2. Body inner_text fallback — visible text for pages that produce
             sparse accessibility trees (e.g. canvas-heavy apps).

        Format:
            URL: <url>
            === Accessibility Tree ===
            [role] name="..." value="..." description="..."
            ...
            === Page Text ===
            <body inner_text>

        BrowserSurfaceAdapter compatibility: the return type is still `str`, so
        no upstream changes are required.  The format is richer than plain text
        but backward-compatible (callers that parse body text still see it in the
        `Page Text` section).
        """
        await self._ensure_started()
        page = self._driver._page
        url = page.url

        a11y_section = await self._accessibility_tree_section(page)

        try:
            body_text = await page.locator("body").inner_text(timeout=self._timeout_ms)
        except Exception:  # noqa: BLE001
            body_text = await page.content()

        return (
            f"URL: {url}\n"
            f"=== Accessibility Tree ===\n{a11y_section}\n"
            f"=== Page Text ===\n{body_text}"
        )

    async def _accessibility_tree_section(self, page: "Any") -> str:
        """Fetch the CDP accessibility tree and render it as a flat text section.

        Uses a raw CDP session (Accessibility.getFullAXTree) because
        Playwright's high-level page.accessibility API was removed in Playwright
        >= 1.44.  The CDP command returns a flat list of all accessibility nodes;
        we filter to interesting nodes (not ignored, role present) and render
        them sorted by nodeId to approximate document order.

        Each node is rendered on one line:
            [role] name="<name>" value="<value>" description="<description>"

        Falls back to a "(accessibility tree unavailable)" string if the CDP
        session cannot be established or the command fails (e.g., sandboxed page
        with no content).
        """
        try:
            cdp_session = await page.context.new_cdp_session(page)
            result = await cdp_session.send("Accessibility.getFullAXTree", {})
            await cdp_session.detach()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "hermes.browser.cdp_cli_adapter.a11y_snapshot_failed",
                extra={"error": str(exc)},
            )
            return "(accessibility tree unavailable)"

        nodes = result.get("nodes", [])
        if not nodes:
            return "(empty accessibility tree)"

        lines: list[str] = []
        _render_cdp_a11y_nodes(nodes, lines)
        return "\n".join(lines)

    async def click(self, ref: str) -> None:
        """Click the element identified by the CSS selector ref."""
        await self._ensure_started()
        page = self._driver._page
        await page.locator(ref).click(timeout=self._timeout_ms)
        logger.debug(
            "hermes.browser.cdp_cli_adapter.clicked",
            extra={"ref": ref},
        )

    async def type_(self, ref: str, text: str) -> None:
        """Fill the input identified by CSS selector ref with text."""
        await self._ensure_started()
        page = self._driver._page
        await page.locator(ref).fill(text, timeout=self._timeout_ms)
        logger.debug(
            "hermes.browser.cdp_cli_adapter.typed",
            extra={"ref": ref, "length": len(text)},
        )

    async def current_url(self) -> str:
        """Return the current page URL."""
        await self._ensure_started()
        return str(self._driver._page.url)

    async def close(self) -> None:
        """Disconnect from the remote browser. Does NOT stop the sandbox Chromium."""
        if not self._started:
            return
        await self._driver.close()
        self._started = False
        logger.info("hermes.browser.cdp_cli_adapter.closed")

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    async def _ensure_started(self) -> None:
        """Lazy start on first operation."""
        if not self._started:
            await self.start()


# ---------------------------------------------------------------------------
# Module-level helper — pure function, no I/O
# ---------------------------------------------------------------------------


def _render_a11y_node(
    node: dict[str, Any],
    lines: list[str],
    *,
    depth: int,
) -> None:
    """Recursively render one Playwright-style accessibility node into `lines`.

    Used for unit testing with synthetic tree dicts (same shape as the old
    Playwright page.accessibility.snapshot() output).  Production code uses
    _render_cdp_a11y_nodes instead (flat CDP node list).

    Each node becomes a single line:
        [role] name="<name>" value="<value>" description="<description>"
    """
    role = node.get("role", "")
    if not role:
        return

    parts: list[str] = [f"{'  ' * depth}[{role}]"]
    for attr in ("name", "value", "description"):
        val = node.get(attr, "")
        if val:
            parts.append(f'{attr}="{val}"')

    lines.append(" ".join(parts))

    for child in node.get("children", []):
        _render_a11y_node(child, lines, depth=depth + 1)


def _render_cdp_a11y_nodes(
    nodes: list[dict[str, Any]],
    lines: list[str],
) -> None:
    """Render a flat CDP Accessibility.getFullAXTree node list into `lines`.

    CDP returns nodes with the shape:
        {nodeId, ignored, role: {value}, name: {value}, ...}

    We filter out ignored/uninteresting nodes and nodes without a role, then
    render each on one line in document order (nodes are already ordered by
    nodeId ascending from the CDP response).

    Format per line:
        [role] name="<name>" value="<value>" description="<description>"
    """
    _INTERESTING_ROLES: frozenset[str] = frozenset({
        "RootWebArea", "button", "textbox", "checkbox", "radio", "combobox",
        "listbox", "option", "link", "menuitem", "tab", "tablist", "dialog",
        "alert", "alertdialog", "form", "group", "heading", "img",
        "list", "listitem", "main", "nav", "region", "search", "table",
        "cell", "row", "columnheader", "rowheader", "StaticText",
    })

    for node in nodes:
        if node.get("ignored", False):
            continue
        role = node.get("role", {}).get("value", "")
        if not role or role == "none":
            continue
        # Only emit nodes with a role we consider interesting OR that have a name.
        name = node.get("name", {}).get("value", "")
        if role not in _INTERESTING_ROLES and not name:
            continue

        parts: list[str] = [f"[{role}]"]
        if name:
            parts.append(f'name="{name}"')
        for attr_key in ("value", "description"):
            attr_val = node.get(attr_key, {})
            val = attr_val.get("value", "") if isinstance(attr_val, dict) else ""
            if val:
                parts.append(f'{attr_key}="{val}"')

        lines.append(" ".join(parts))
