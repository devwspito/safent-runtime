"""Static consistency of the .enterprise marker wiring across code + systemd.

Regression (2026-07-08): pairing turned an instance into an associate but never
created the /var/lib/hermes/instance/.enterprise marker, and no unit watched for
it — so hermes-config-sync.service (gated by ConditionPathExists on that marker)
stayed inert after pairing until a reboot. The fix is two parts that MUST agree
on the exact marker path:
  1. pairing_service.write_enterprise_marker() creates it on pair.
  2. hermes-config-sync.path watches it and starts hermes-config-sync.service.
This test pins that they reference the same path and each other, so the wiring
can't silently drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.instance.pairing_service import _DEFAULT_ENTERPRISE_MARKER

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SYSTEMD = _REPO_ROOT / "ops/agents-os-edition/systemd"


def _read(name: str) -> str:
    return (_SYSTEMD / name).read_text(encoding="utf-8")


def test_marker_path_constant_matches_service_condition() -> None:
    service = _read("hermes-config-sync.service")
    assert f"ConditionPathExists={_DEFAULT_ENTERPRISE_MARKER}" in service, (
        "config-sync.service must gate on the SAME marker the code writes"
    )


def test_path_unit_watches_the_marker_and_starts_the_service() -> None:
    path_unit = _read("hermes-config-sync.path")
    assert f"PathExists={_DEFAULT_ENTERPRISE_MARKER}" in path_unit, (
        "the .path unit must watch the SAME marker the code writes"
    )
    assert "Unit=hermes-config-sync.service" in path_unit, (
        "the .path unit must trigger config-sync.service"
    )
    assert "WantedBy=" in path_unit, "the .path unit must be installable (enabled at build)"


def test_containerfile_enables_the_path_unit() -> None:
    containerfile = (_REPO_ROOT / "ops/container/Containerfile").read_text(encoding="utf-8")
    assert "hermes-config-sync.path" in containerfile, (
        "the Containerfile must `systemctl enable` the .path unit, else pairing "
        "never auto-starts config-sync"
    )
