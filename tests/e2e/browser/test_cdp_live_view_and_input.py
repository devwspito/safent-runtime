"""E2E tests — seam B (spec 012): CDP live-view + take-control against jailed Chromium.

Markers:
  requires_openshell — needs Chromium reachable at CDP 127.0.0.1:9222.

Tests:
  1. Live-view: CdpScreencastSource receives N real frames while navigating.
     Saves one frame to /tmp for evidence.
  2. Take-control: CdpInputAdapter injects a mouse click + text via CDP Input.
     Verifies the text lands in a page input element (observable DOM effect).
  3. Structural check: CdpScreencastSource satisfies FrameSourcePort;
     CdpInputAdapter satisfies SeatInputEffectorPort.

Run:
    .venv/bin/python -m pytest \\
      tests/e2e/browser/test_cdp_live_view_and_input.py \\
      -m requires_openshell -s -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from hermes.browser.infrastructure.cdp_input_adapter import CdpInputAdapter
from hermes.browser.infrastructure.cdp_screencast_source import CdpScreencastSource
from hermes.browser.infrastructure.cdp_playwright_driver import CdpPlaywrightDriver
from hermes.shell_server.mirror.frame_source_port import FrameSourcePort
from hermes.shell_server.mirror.input_effector_port import SeatInputEffectorPort

pytestmark = pytest.mark.requires_openshell

CDP_URL = os.environ.get("HERMES_CDP_URL", "http://127.0.0.1:9222")

# httpbin.org/forms/post — has a text input we can fill and verify.
FORM_URL = "https://httpbin.org/forms/post"
_MIN_FRAMES = 2
_FRAME_WAIT_S = 3.0
_FRAME_SAVE_PATH = Path("/tmp/cdp_e2e_screencast_frame.jpg")
_INPUT_SELECTOR = 'input[name="custname"]'
_TEST_TEXT = "HERMES_CDP_E2E_OK"


# ---------------------------------------------------------------------------
# Test 1: Live-view — receive real JPEG frames via CDP screencast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdp_screencast_receives_real_frames() -> None:
    """CdpScreencastSource emits non-empty JPEG frames from the jailed Chromium.

    Evidence: at least _MIN_FRAMES frames, each > 1 KiB JPEG;
    first frame saved to /tmp for manual inspection.
    """
    driver = CdpPlaywrightDriver(cdp_url=CDP_URL, timeout_ms=20_000)
    await driver.start()

    # Use the page that the driver opens in the existing browser context.
    page = driver._page
    source = CdpScreencastSource(page=page, quality=60, max_width=1280, max_height=720)

    try:
        await source.start()

        # Navigate to trigger new frames (damage events).
        await page.goto(FORM_URL, timeout=15_000)
        await asyncio.sleep(_FRAME_WAIT_S)

        frame_bytes, size = source.latest()
        frame_count = source.frame_count

        print(
            f"\n[E2E] screencast frames={frame_count} "
            f"latest_size={len(frame_bytes) if frame_bytes else 0} bytes "
            f"dimensions={size}"
        )

    finally:
        await source._async_stop()
        await driver.close()

    # Assertions
    assert frame_count >= _MIN_FRAMES, (
        f"Expected >= {_MIN_FRAMES} frames, got {frame_count}. "
        "Chrome may not have sent frames — check CDP endpoint."
    )
    assert frame_bytes is not None, "No frame stored after navigation"
    assert len(frame_bytes) > 1024, (
        f"Frame is too small ({len(frame_bytes)} bytes) — likely blank or error"
    )
    # Minimal JPEG header check: starts with SOI marker 0xFF 0xD8
    assert frame_bytes[:2] == b"\xff\xd8", (
        f"Stored bytes do not look like JPEG (header: {frame_bytes[:4].hex()})"
    )

    # Save for evidence
    _FRAME_SAVE_PATH.write_bytes(frame_bytes)
    print(f"[E2E] Frame saved to {_FRAME_SAVE_PATH} ({len(frame_bytes)} bytes)")


# ---------------------------------------------------------------------------
# Test 2: Take-control — inject click + text via CDP Input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdp_input_adapter_injects_click_and_text() -> None:
    """CdpInputAdapter click + text injection produces observable DOM effect.

    Strategy: navigate to httpbin.org/forms/post, click the custname input,
    type a sentinel string via async_type_text, read back input value via
    Playwright locator.input_value — must match exactly.
    """
    driver = CdpPlaywrightDriver(cdp_url=CDP_URL, timeout_ms=20_000)
    await driver.start()

    page = driver._page

    try:
        await page.goto(FORM_URL, timeout=15_000)
        await asyncio.sleep(0.3)

        # Open a CDP session for the input adapter
        session = await page.context.new_cdp_session(page)
        adapter = CdpInputAdapter(session=session)

        # Find the custname input and its bounding box (logical px in page coords)
        box = await page.locator(_INPUT_SELECTOR).bounding_box(timeout=10_000)
        assert box is not None, f"Selector {_INPUT_SELECTOR!r} not found"

        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2

        # Click to focus the input
        await adapter.async_mouse_click(cx, cy, button="left")
        await asyncio.sleep(0.1)

        # Clear any pre-existing value and type our sentinel
        await page.locator(_INPUT_SELECTOR).clear(timeout=5_000)
        await adapter.async_type_text(_TEST_TEXT)
        await asyncio.sleep(0.2)

        value = await page.locator(_INPUT_SELECTOR).input_value(timeout=5_000)
        print(f"\n[E2E] Input value after CDP injection: {value!r}")

    finally:
        try:
            await session.detach()
        except Exception:  # noqa: BLE001
            pass
        await driver.close()

    assert value == _TEST_TEXT, (
        f"Expected {_TEST_TEXT!r}, got {value!r}. "
        "CDP input injection did not produce the expected DOM effect."
    )


# ---------------------------------------------------------------------------
# Test 3: Structural compliance (no real browser needed but gated by marker)
# ---------------------------------------------------------------------------


def test_cdp_screencast_source_satisfies_frame_source_port() -> None:
    """CdpScreencastSource structurally satisfies FrameSourcePort (Protocol check)."""
    from unittest.mock import MagicMock

    source = CdpScreencastSource(page=MagicMock())
    assert isinstance(source, FrameSourcePort), (
        "CdpScreencastSource must implement FrameSourcePort"
    )


def test_cdp_input_adapter_satisfies_seat_input_effector_port() -> None:
    """CdpInputAdapter structurally satisfies SeatInputEffectorPort (Protocol check)."""
    from unittest.mock import MagicMock

    session = MagicMock()
    adapter = CdpInputAdapter(session=session)
    assert isinstance(adapter, SeatInputEffectorPort), (
        "CdpInputAdapter must implement SeatInputEffectorPort"
    )
