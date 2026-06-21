"""Tests del CLI `hermes` (spec 003 FR-012)."""

from __future__ import annotations

import json
import sys
from io import StringIO

import pytest

from hermes.cli.main import run

pytestmark = pytest.mark.unit


def _run_capturing(argv: list[str]) -> tuple[int, str]:
    out = StringIO()
    saved = sys.stdout
    sys.stdout = out
    try:
        code = run(argv)
    finally:
        sys.stdout = saved
    return code, out.getvalue()


class TestStatusCommand:
    def test_status_returns_zero(self) -> None:
        code, _ = _run_capturing(["status"])
        assert code == 0

    def test_status_json_is_parseable(self) -> None:
        code, output = _run_capturing(["--json", "status"])
        assert code == 0
        parsed = json.loads(output)
        assert parsed["exit_code"] == 0
        assert "runtime" in parsed["payload"]


class TestTelemetryCommand:
    def test_telemetry_default_is_disabled(self) -> None:
        code, output = _run_capturing(["telemetry", "status"])
        assert code == 0
        assert "DESACTIVADA" in output
        assert "opt-in puro" in output

    def test_telemetry_enable_reports_charter_compliance(self) -> None:
        code, output = _run_capturing(["telemetry", "enable"])
        assert code == 0
        assert "ACTIVADA" in output
        assert "datos del cliente" in output


class TestSuspendCommand:
    def test_suspend_without_yes_blocks(self) -> None:
        code, output = _run_capturing(["suspend"])
        assert code == 1
        assert "24/7" in output or "always_on" in output or "always_on" in output.lower()

    def test_suspend_with_yes_acknowledges(self) -> None:
        code, output = _run_capturing(["suspend", "--yes"])
        assert code == 0
        assert "FR-041" in output or "Suspend" in output


class TestTenantCommand:
    def test_status_when_no_binding(self) -> None:
        code, output = _run_capturing(["tenant", "status"])
        assert code == 0
        assert "Sin tenant" in output

    def test_bind_requires_tenant_id(self) -> None:
        code, output = _run_capturing(["tenant", "bind"])
        assert code == 2
        assert "tenant_id" in output

    def test_bind_with_id_succeeds(self) -> None:
        code, output = _run_capturing(
            ["tenant", "bind", "11111111-1111-1111-1111-111111111111"]
        )
        assert code == 0
        assert "vinculado" in output

    def test_unbind_acknowledges(self) -> None:
        code, output = _run_capturing(["tenant", "unbind"])
        assert code == 0
        assert "desvinculado" in output
