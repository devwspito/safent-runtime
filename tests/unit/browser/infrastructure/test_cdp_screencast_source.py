"""Unit tests for CdpScreencastSource — mock CDP session, no real browser.

Coverage:
  - latest() returns (None, (0,0)) before any frame arrives.
  - _handle_screencast_frame stores decoded JPEG and (w, h).
  - _handle_screencast_frame fires ack via session.send.
  - _handle_screencast_frame ignores frames with empty data.
  - _handle_screencast_frame ignores malformed b64 data gracefully.
  - frame_count increments on each valid frame.
  - start() calls Page.startScreencast with correct params.
  - stop() is idempotent (no error on double stop).
  - FrameSourcePort structural compliance: latest() and stop() present.
"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from hermes.browser.infrastructure.cdp_screencast_source import (
    CdpScreencastSource,
    CdpScreencastSourceError,
)
from hermes.shell_server.mirror.frame_source_port import FrameSourcePort


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_session() -> MagicMock:
    """Return a mock CDPSession with async send and on()."""
    session = MagicMock()
    session.send = AsyncMock(return_value=None)
    session.detach = AsyncMock(return_value=None)
    # on() is synchronous — stores handler; we call it manually in tests.
    session.on = MagicMock()
    return session


def _make_mock_page(session: MagicMock) -> MagicMock:
    """Return a mock Playwright Page whose context creates our mock session."""
    ctx = MagicMock()
    ctx.new_cdp_session = AsyncMock(return_value=session)
    page = MagicMock()
    page.context = ctx
    return page


def _jpeg_b64(content: bytes = b"JPEG_FAKE_DATA") -> str:
    return base64.b64encode(content).decode()


# ---------------------------------------------------------------------------
# latest() before any frame
# ---------------------------------------------------------------------------


def test_latest_returns_none_before_start() -> None:
    source = CdpScreencastSource(page=MagicMock())
    data, size = source.latest()
    assert data is None
    assert size == (0, 0)


# ---------------------------------------------------------------------------
# _handle_screencast_frame — frame storage and ack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frame_stored_and_ack_sent() -> None:
    session = _make_mock_session()
    page = _make_mock_page(session)
    source = CdpScreencastSource(page=page)
    source._session = session
    source._running = True

    jpeg_content = b"\xff\xd8\xff" + b"\x00" * 100  # minimal JPEG header
    params = {
        "data": base64.b64encode(jpeg_content).decode(),
        "sessionId": 7,
        "metadata": {"deviceWidth": 1280, "deviceHeight": 720},
    }

    source._handle_screencast_frame(params)
    await asyncio.sleep(0)  # let ensure_future fire

    data, size = source.latest()
    assert data == jpeg_content
    assert size == (1280, 720)

    # Ack must be sent with correct sessionId
    session.send.assert_awaited_once_with(
        "Page.screencastFrameAck", {"sessionId": 7}
    )


@pytest.mark.asyncio
async def test_frame_count_increments() -> None:
    session = _make_mock_session()
    page = _make_mock_page(session)
    source = CdpScreencastSource(page=page)
    source._session = session
    source._running = True

    for i in range(3):
        source._handle_screencast_frame({
            "data": _jpeg_b64(b"data" + str(i).encode()),
            "sessionId": i,
            "metadata": {},
        })

    await asyncio.sleep(0)  # let ensure_future tasks run
    assert source.frame_count == 3


def test_empty_data_ignored() -> None:
    session = _make_mock_session()
    page = _make_mock_page(session)
    source = CdpScreencastSource(page=page)
    source._session = session

    source._handle_screencast_frame({"data": "", "sessionId": 1, "metadata": {}})

    data, _ = source.latest()
    assert data is None
    assert source.frame_count == 0


def test_malformed_b64_ignored() -> None:
    session = _make_mock_session()
    page = _make_mock_page(session)
    source = CdpScreencastSource(page=page)
    source._session = session

    # not valid base64
    source._handle_screencast_frame({
        "data": "!!!NOT_VALID_B64!!!",
        "sessionId": 1,
        "metadata": {},
    })

    data, _ = source.latest()
    assert data is None
    assert source.frame_count == 0


# ---------------------------------------------------------------------------
# start() — CDP params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_sends_correct_cdp_params() -> None:
    session = _make_mock_session()
    page = _make_mock_page(session)
    source = CdpScreencastSource(page=page, quality=80, max_width=640, max_height=480)

    await source.start()

    session.send.assert_awaited_once_with(
        "Page.startScreencast",
        {
            "format": "jpeg",
            "everyNthFrame": 1,
            "quality": 80,
            "maxWidth": 640,
            "maxHeight": 480,
        },
    )
    assert source._running is True


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    session = _make_mock_session()
    page = _make_mock_page(session)
    source = CdpScreencastSource(page=page)

    await source.start()
    await source.start()  # second call must not call startScreencast again

    assert session.send.await_count == 1


@pytest.mark.asyncio
async def test_start_raises_when_cdp_session_fails() -> None:
    ctx = MagicMock()
    ctx.new_cdp_session = AsyncMock(side_effect=RuntimeError("CDP unavailable"))
    page = MagicMock()
    page.context = ctx
    source = CdpScreencastSource(page=page)

    with pytest.raises(CdpScreencastSourceError, match="CDP unavailable"):
        await source.start()


# ---------------------------------------------------------------------------
# stop() — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_sends_stopScreencast_and_detach() -> None:
    session = _make_mock_session()
    page = _make_mock_page(session)
    source = CdpScreencastSource(page=page)
    source._session = session
    source._running = True

    await source._async_stop()

    # stopScreencast + detach
    calls = [c.args[0] for c in session.send.await_args_list]
    assert "Page.stopScreencast" in calls
    session.detach.assert_awaited_once()
    assert source._session is None


@pytest.mark.asyncio
async def test_stop_is_idempotent_no_session() -> None:
    source = CdpScreencastSource(page=MagicMock())
    # _session is None, _running is False → should not raise
    await source._async_stop()


# ---------------------------------------------------------------------------
# FrameSourcePort structural compliance
# ---------------------------------------------------------------------------


def test_conforms_to_frame_source_port() -> None:
    source = CdpScreencastSource(page=MagicMock())
    assert isinstance(source, FrameSourcePort), (
        "CdpScreencastSource must satisfy FrameSourcePort protocol"
    )
