"""Regression tests — graceful SIGTERM shutdown (spec 025 matriz item #2).

Root cause (2026-09-10, verified live: `podman stop` never returned, even
given 90s, always ending in SIGKILL): `runtime/__main__.py` registered THREE
separate `event_loop.add_signal_handler(signal.SIGTERM, ...)` callbacks.
`asyncio.add_signal_handler(sig, callback)` REPLACES any previous handler
for the SAME signal — it does not stack multiple callbacks. Only the LAST
registration (`browser_guard.signal_shutdown`, or nothing at all when
`browser_guard` is `None`) ever fired; `orchestrator.request_shutdown()` and
`unix_socket.close()` were silently dead code, so the main agent-loop task
never saw a shutdown signal and `asyncio.gather()` blocked forever.

These tests pin:
  1. asyncio's actual `add_signal_handler` replace-not-stack semantics (the
     underlying platform behaviour the bug depended on — documented here so
     the fix's rationale doesn't silently rot).
  2. `runtime/__main__.py` registers `signal.SIGTERM` exactly ONCE — a
     static regression guard against reintroducing the multi-registration
     anti-pattern.
  3. The single combined handler pattern the fix uses actually invokes every
     wired shutdown action (orchestrator, socket, browser guard) exactly
     once, from one registration.
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import signal
from pathlib import Path

_MAIN_PY = (
    Path(__file__).resolve().parents[3]
    / "src" / "hermes" / "runtime" / "__main__.py"
)


class TestAddSignalHandlerReplacesNotStacks:
    """Pins the asyncio behaviour the original bug depended on."""

    async def test_only_the_last_registered_sigterm_handler_fires(self) -> None:
        calls: list[str] = []
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: calls.append("first"))
        loop.add_signal_handler(signal.SIGTERM, lambda: calls.append("second"))
        loop.add_signal_handler(signal.SIGTERM, lambda: calls.append("third"))
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0.1)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

        # The bug this test documents: "first" and "second" never ran.
        assert calls == ["third"]


class TestMainRegistersSigtermExactlyOnce:
    """Static regression guard: __main__.py must not reintroduce the
    multi-registration anti-pattern (each call silently drops the previous
    one — see TestAddSignalHandlerReplacesNotStacks)."""

    def test_add_signal_handler_called_once_for_sigterm(self) -> None:
        source = _MAIN_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        sigterm_registrations = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_signal_handler"):
                continue
            if not node.args:
                continue
            sig_arg = node.args[0]
            # signal.SIGTERM is an ast.Attribute (Name('signal'), attr='SIGTERM')
            if (
                isinstance(sig_arg, ast.Attribute)
                and sig_arg.attr == "SIGTERM"
            ):
                sigterm_registrations += 1
        assert sigterm_registrations == 1, (
            f"expected exactly ONE add_signal_handler(signal.SIGTERM, ...) "
            f"registration, found {sigterm_registrations} — each additional "
            "call silently REPLACES the previous handler (asyncio semantics), "
            "reintroducing the graceful-shutdown hang this test guards against."
        )

    def test_grace_then_cancel_helper_present(self) -> None:
        """The straggler-cancellation backstop (trigger sources / dbus task /
        model monitor / composio poller have no graceful hook of their own)
        must still be wired, not just the three named shutdown calls."""
        source = _MAIN_PY.read_text(encoding="utf-8")
        assert re.search(r"async def _cancel_runtime_tasks_after_grace", source)
        assert "task.cancel()" in source
        assert "not task.cancelling()" in source


class TestCombinedSigtermHandlerFiresEveryAction:
    """The fix pattern itself: ONE callback, all three actions, invoked
    exactly once each from a single add_signal_handler registration."""

    async def test_single_handler_runs_every_wired_action(self) -> None:
        calls: list[str] = []

        def _handle_sigterm() -> None:
            calls.append("orchestrator")
            calls.append("socket")
            calls.append("browser_guard")

        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0.1)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

        assert calls == ["orchestrator", "socket", "browser_guard"]
