"""Unit-test env defaults.

Los unit-tests corren SIN systemd privilegiado / sin el exec-launcher root, así
que el confinamiento del terminal por systemd-run no es aplicable aquí. Forzamos
el modo RAW documentado (HERMES_TERMINAL_SCOPE=0) para ejercitar la lógica del
adapter (capture/replay) sin depender del cage — el hardening real (launcher /
systemd-run con privilegio) se valida en integración/imagen baked, no en unit.
"""
import os

os.environ.setdefault("HERMES_TERMINAL_SCOPE", "0")


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _fresh_write_tool_failure_counters():
    """The per-cycle breaker counters are thread-local; tests that call the engine
    directly never run the cycle reset, so a previous test's failures would trip
    the one-strike breaker in an unrelated test."""
    from hermes.runtime.conversation_task_registry import reset_write_tool_failures

    reset_write_tool_failures()
    yield
    reset_write_tool_failures()
