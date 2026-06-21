"""Regression: the first-boot gate must wait for the shell-server.

The graphical shell (GDM-autologin kiosk) can start before the shell-server
(a system service that needs master.key + a FastAPI boot). The boot gate used
to make a single wizard_status() call and, on any error, fall back to
``first_boot_complete=True`` — which permanently skipped the onboarding wizard
on a fresh install whenever the backend wasn't up yet (a startup race).

These tests pin the corrected behaviour: retry on connection/HTTP errors until
the backend answers, never hang, and don't penalise returning users.
"""

from __future__ import annotations

import urllib.error

import pytest

from hermes.shell.presentation.gtk4 import app as app_mod


class _ScriptedClient:
    """wizard_status() yields scripted outcomes; repeats the last one."""

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self._last = outcomes[-1]
        self.calls = 0

    def wizard_status(self) -> dict:
        self.calls += 1
        outcome = self._outcomes.pop(0) if self._outcomes else self._last
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def captured_decision(monkeypatch):
    """Capture the boot decision instead of scheduling it on the GTK loop."""
    box: dict = {}

    class _FakeGLib:
        @staticmethod
        def idle_add(_fn, arg):  # signature matches GLib.idle_add(callable, arg)
            box["decision"] = arg
            return False

    monkeypatch.setattr(app_mod, "GLib", _FakeGLib)
    monkeypatch.setattr(app_mod.time, "sleep", lambda _s: None)
    return box


def _app(client) -> app_mod.HermesShellApplication:
    app = app_mod.HermesShellApplication(windowed=True, mock_runtime=True)
    app._client = client
    return app


def test_retries_until_backend_ready_then_shows_wizard(captured_decision):
    refused = urllib.error.URLError("Connection refused")
    client = _ScriptedClient([refused, refused, {"first_boot_complete": False}])

    _app(client)._thread_check_wizard_status()

    assert client.calls == 3
    # fresh install → wizard MUST be shown (decision is "first_boot_complete")
    assert captured_decision["decision"] is False


def test_returning_user_answers_first_try_no_retry(captured_decision):
    client = _ScriptedClient([{"first_boot_complete": True}])

    _app(client)._thread_check_wizard_status()

    assert client.calls == 1
    assert captured_decision["decision"] is True


def test_bounded_when_backend_never_ready(captured_decision, monkeypatch):
    # Always refuses → must give up at the deadline and default to the shell,
    # never hang the boot.
    monkeypatch.setattr(app_mod, "_BOOT_GATE_TIMEOUT_S", 0.0)
    client = _ScriptedClient([urllib.error.URLError("Connection refused")])

    _app(client)._thread_check_wizard_status()

    assert captured_decision["decision"] is True


def test_http_error_during_warmup_is_retried(captured_decision):
    # A transient 503 (master.key/DB still warming) is an HTTPError (subclass of
    # URLError) — it must be retried, not treated as "onboarded".
    http_503 = urllib.error.HTTPError(
        url="http://x/api/v1/wizard/status", code=503, msg="warming", hdrs=None, fp=None
    )
    client = _ScriptedClient([http_503, {"first_boot_complete": False}])

    _app(client)._thread_check_wizard_status()

    assert client.calls == 2
    assert captured_decision["decision"] is False


def test_non_network_error_degrades_to_shell(captured_decision):
    # A non-network error (e.g. malformed payload) is not a startup race —
    # degrade to the shell immediately rather than spinning to the deadline.
    client = _ScriptedClient([ValueError("bad json")])

    _app(client)._thread_check_wizard_status()

    assert client.calls == 1
    assert captured_decision["decision"] is True
