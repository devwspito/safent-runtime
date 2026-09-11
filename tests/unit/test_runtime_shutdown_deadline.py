"""Hard-stop proof uses an isolated spawned process, never a running service."""

import threading
import time
from multiprocessing import get_context
from pathlib import Path

import pytest

from hermes.runtime.managed_llm_lifecycle import Generation, ProcessAdmission
from hermes.runtime.shutdown_deadline import ShutdownDeadline

pytestmark = pytest.mark.unit


def _stuck_runtime(started):
    # Models native synchronous run_conversation remaining blocked after its
    # asyncio Future was cancelled. The non-daemon thread cannot be joined.
    thread = threading.Thread(target=lambda: threading.Event().wait())
    thread.start()
    started.set()
    ShutdownDeadline(seconds=0.2).arm()
    thread.join()


def _stuck_interrupt(started):
    guard = ProcessAdmission(Path("unused-fixture.db"), Generation(1, "fixture", "local", False))

    def callback():
        started.set()
        threading.Event().wait()

    guard.register_interrupt(callback)
    ShutdownDeadline(seconds=0.2).arm()
    guard.close()
    guard.interrupt_inflight()


@pytest.mark.parametrize("target", [_stuck_runtime, _stuck_interrupt])
def test_hard_deadline_terminates_stuck_native_thread_process(target):
    context = get_context("spawn")
    started = context.Event()
    process = context.Process(target=target, args=(started,))
    before = time.monotonic()
    process.start()
    assert started.wait(timeout=5)
    process.join(timeout=3)
    try:
        assert not process.is_alive()
        assert process.exitcode == 75
        assert time.monotonic() - before < 4
    finally:
        if process.is_alive():
            process.kill()
            process.join()


def test_deadline_is_idempotent_and_does_not_reset_on_repeated_shutdown():
    calls = []
    expired = threading.Event()

    def stop(code):
        calls.append(code)
        expired.set()

    deadline = ShutdownDeadline(seconds=0.05, exit_process=stop)
    deadline.arm()
    first = deadline._timer
    deadline.arm()
    assert first is deadline._timer
    assert expired.wait(timeout=1)
    assert calls == [75]


def test_existing_service_reaps_entire_cgroup_with_independent_deadline():
    unit = Path("ops/agents-os-edition/systemd/hermes-runtime.service").read_text()
    assert "KillMode=control-group" in unit
    assert "TimeoutStopSec=10" in unit
    assert "SendSIGKILL=yes" in unit
    assert "Restart=always" in unit
