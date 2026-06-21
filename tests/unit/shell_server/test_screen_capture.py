"""Tests del cimiento screen_capture (dominio + service con fake backend)."""

from __future__ import annotations

import pytest

from hermes.shell_server.screen_capture.domain import (
    CaptureTarget,
    CaptureTargetKind,
    Frame,
)
from hermes.shell_server.screen_capture.fake import FakeScreenCaptureBackend
from hermes.shell_server.screen_capture.service import ScreenCaptureService

pytestmark = pytest.mark.unit


class TestDomain:
    def test_monitor_target(self) -> None:
        t = CaptureTarget.monitor("Virtual-1")
        assert t.kind is CaptureTargetKind.MONITOR
        assert t.monitor_connector == "Virtual-1"

    def test_window_target(self) -> None:
        t = CaptureTarget.window(42)
        assert t.kind is CaptureTargetKind.WINDOW
        assert t.window_id == 42

    def test_frame_stride_and_blank(self) -> None:
        blank = Frame(width=4, height=2, data=bytes(4 * 2 * 4), sequence=1)
        assert blank.stride == 16
        assert blank.is_blank()
        painted = Frame(
            width=4, height=2, data=bytes([1]) + bytes(4 * 2 * 4 - 1), sequence=2
        )
        assert not painted.is_blank()


class TestService:
    def test_start_fans_out_frames_to_subscribers(self) -> None:
        backend = FakeScreenCaptureBackend(frames=3)
        svc = ScreenCaptureService(backend=backend)
        received: list[Frame] = []
        svc.subscribe(received.append)

        assert not svc.is_active
        svc.start(CaptureTarget.monitor("Virtual-1"))
        assert svc.is_active
        assert len(received) == 3
        assert [f.sequence for f in received] == [1, 2, 3]
        assert all(not f.is_blank() for f in received)

    def test_latest_frame(self) -> None:
        backend = FakeScreenCaptureBackend(frames=2)
        svc = ScreenCaptureService(backend=backend)
        svc.start(CaptureTarget.monitor("Virtual-1"))
        latest = svc.latest_frame()
        assert latest is not None
        assert latest.sequence == 2

    def test_unsubscribe_stops_delivery(self) -> None:
        backend = FakeScreenCaptureBackend(frames=2)
        svc = ScreenCaptureService(backend=backend)
        received: list[Frame] = []
        cb = received.append
        svc.subscribe(cb)
        svc.unsubscribe(cb)
        svc.start(CaptureTarget.monitor("Virtual-1"))
        assert received == []

    def test_start_idempotent(self) -> None:
        backend = FakeScreenCaptureBackend(frames=1)
        svc = ScreenCaptureService(backend=backend)
        count: list[int] = []
        svc.subscribe(lambda f: count.append(f.sequence))
        svc.start(CaptureTarget.monitor("Virtual-1"))
        svc.start(CaptureTarget.monitor("Virtual-1"))  # no-op
        assert len(count) == 1

    def test_stop_resets_active(self) -> None:
        svc = ScreenCaptureService(backend=FakeScreenCaptureBackend(frames=1))
        svc.start(CaptureTarget.monitor("Virtual-1"))
        svc.stop()
        assert not svc.is_active
        svc.stop()  # idempotent
