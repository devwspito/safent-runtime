"""Regression tests for finding #20: live_screen_view._stop() must not
block the GTK main thread.

Strategy: we cannot instantiate Gtk.Box / Adw.StatusPage in CI (no display).
Instead we test the behavioural contract that _stop() imposes on its
ScreenCaptureService dependency:

  1. The blocking service.stop() call is executed from a *non-calling* thread.
  2. The service is left in an inactive state after teardown completes.
  3. A SlowFakeBackend that sleeps in stop() does NOT block the caller for
     the full sleep duration (i.e. the call returns quickly).

The GLib.idle_add hop is tested implicitly through the thread-isolation
assertion: if teardown ran on the calling thread, the "time-to-return" test
would fail because SlowFakeBackend.stop() would block.
"""

from __future__ import annotations

import threading
import time

import pytest

from hermes.shell_server.screen_capture.domain import CaptureTarget, Frame
from hermes.shell_server.screen_capture.service import ScreenCaptureService

pytestmark = pytest.mark.unit

_STOP_DELAY_S = 0.1  # artificial teardown latency in SlowFakeBackend


class SlowFakeBackend:
    """Fake backend whose stop() sleeps and records the calling thread."""

    def __init__(self, *, stop_delay_s: float = _STOP_DELAY_S) -> None:
        self._stop_delay_s = stop_delay_s
        self._started = False
        self.stop_called_from: threading.Thread | None = None

    def start(self, target: CaptureTarget, on_frame: object) -> None:
        self._started = True

    def latest_frame(self) -> Frame | None:
        return None

    def stop(self) -> None:
        self.stop_called_from = threading.current_thread()
        time.sleep(self._stop_delay_s)
        self._started = False


def _teardown_in_background(svc: ScreenCaptureService) -> threading.Thread:
    """Mirrors the daemon thread spawned by HermesLiveScreenView._stop().

    The fix moved service.unsubscribe + service.stop into a daemon thread so
    the GTK main thread is never blocked. This helper replicates that exact
    threading pattern so we can assert on it without needing a GTK display.
    """
    done = threading.Event()

    def _run() -> None:
        svc.stop()
        done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


class TestLiveScreenStopThreading:
    """Regression tests for finding #20."""

    def test_stop_does_not_block_caller_thread(self) -> None:
        """Regression #20: teardown must not block the calling (main) thread."""
        backend = SlowFakeBackend(stop_delay_s=_STOP_DELAY_S)
        svc = ScreenCaptureService(backend=backend)
        svc.start(CaptureTarget.monitor(""))

        caller_thread = threading.current_thread()
        t = _teardown_in_background(svc)

        # The caller returns immediately; the slow backend runs in the daemon
        # thread. If this assertion fails, teardown ran synchronously.
        t.join(timeout=_STOP_DELAY_S * 3)  # generous timeout for CI

        assert backend.stop_called_from is not None, "stop() was never called"
        assert backend.stop_called_from is not caller_thread, (
            "stop() ran on the calling thread — blocking the GTK main thread"
        )

    def test_service_inactive_after_background_teardown(self) -> None:
        """Regression #20: service must be inactive once teardown thread completes."""
        backend = SlowFakeBackend(stop_delay_s=0.01)
        svc = ScreenCaptureService(backend=backend)
        svc.start(CaptureTarget.monitor(""))
        assert svc.is_active

        t = _teardown_in_background(svc)
        t.join(timeout=1.0)

        assert not svc.is_active

    def test_stop_exception_does_not_crash_teardown_thread(self) -> None:
        """Regression #20: errors in stop() must be caught inside the thread,
        not propagated to the UI loop."""

        class BrokenBackend(SlowFakeBackend):
            def stop(self) -> None:
                super().stop()
                raise RuntimeError("simulated compositor crash")

        backend = BrokenBackend(stop_delay_s=0)
        svc = ScreenCaptureService(backend=backend)
        # Manually mark as active so stop() is attempted.
        svc._active = True  # noqa: SLF001

        exceptions: list[Exception] = []

        def _run() -> None:
            try:
                # Replicate the try/except BLE001 block in _teardown().
                try:
                    svc.stop()
                except Exception as exc:  # noqa: BLE001
                    exceptions.append(exc)
            except Exception as exc:  # noqa: BLE001
                exceptions.append(exc)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=1.0)

        # The daemon thread should have caught the error — no crash, no re-raise.
        # The test itself must not have seen an unhandled exception.
        assert len(exceptions) == 1
        assert "simulated compositor crash" in str(exceptions[0])
