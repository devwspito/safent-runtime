"""Regression tests for the hermes-bootc-updater oneshot entrypoint.

Guards the FR-008/FR-009 OTA check+stage contract:
  - no managed channel configured  -> exit 0 (valid idle state, not a crash)
  - channel configured             -> fetch_and_stage(image_ref) is called
  - unknown subcommand             -> exit 2
  - bootc binary unavailable       -> exit 0 (timer must not crash-loop)
"""

from __future__ import annotations

import sys

import hermes.bootc_updater_service.__main__ as svc
from hermes.agents_os.infrastructure.bootc_updater import BootcCommandError

_SRC = "hermes.agents_os.infrastructure.bootc_updater.BootcUpdater"


class _FakeStatus:
    booted_digest = "sha256:abc"
    staged_digest = None


class _FakeUpdater:
    def __init__(self, *args, **kwargs) -> None:
        self.staged: list[str] = []

    def status(self) -> _FakeStatus:
        return _FakeStatus()

    def fetch_and_stage(self, image_ref: str) -> None:
        self.staged.append(image_ref)


def test_no_channel_returns_zero(monkeypatch):
    monkeypatch.delenv("HERMES_UPDATE_IMAGE_REF", raising=False)
    monkeypatch.setattr(sys, "argv", ["hermes-bootc-updater", "check-and-stage"])
    monkeypatch.setattr(_SRC, _FakeUpdater)
    assert svc.main() == 0


def test_channel_set_triggers_stage(monkeypatch):
    captured: dict[str, str] = {}

    class _Cap(_FakeUpdater):
        def fetch_and_stage(self, image_ref: str) -> None:
            captured["ref"] = image_ref

    monkeypatch.setenv("HERMES_UPDATE_IMAGE_REF", "quay.io/x/y@sha256:dead")
    monkeypatch.setattr(sys, "argv", ["x", "check-and-stage"])
    monkeypatch.setattr(_SRC, _Cap)
    assert svc.main() == 0
    assert captured["ref"].endswith("sha256:dead")


def test_unknown_subcommand_returns_two(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["x", "bogus"])
    assert svc.main() == 2


def test_status_unavailable_returns_zero(monkeypatch):
    class _Boom(_FakeUpdater):
        def status(self):
            raise BootcCommandError("bootc not found")

    monkeypatch.delenv("HERMES_UPDATE_IMAGE_REF", raising=False)
    monkeypatch.setattr(sys, "argv", ["x", "check-and-stage"])
    monkeypatch.setattr(_SRC, _Boom)
    assert svc.main() == 0
