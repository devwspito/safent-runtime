"""Unit tests for headless-safe helpers in tasks_view.py.

The GTK4 widget itself cannot be instantiated in a headless CI environment,
but the pure datetime-formatting helpers are side-effect-free and fully
testable without a display.

This file imports ONLY the functions — not the Gtk/Adw classes — by patching
the gi.require_version / gi.repository imports to no-ops before the module is
loaded, so the suite stays headless-safe.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Headless guard — stub out gi/GTK so the module can be imported without
# a running display.  This mirrors the pattern used in test_markdown_render.py.
# ---------------------------------------------------------------------------

def _install_gi_stub() -> None:
    """Install minimal gi stubs if gi is not available (headless CI)."""
    if "gi" in sys.modules:
        return

    gi_mod = types.ModuleType("gi")
    gi_mod.require_version = lambda *a, **kw: None  # type: ignore[attr-defined]

    repo_mod = types.ModuleType("gi.repository")

    def _make_stub(name: str):
        stub = types.ModuleType(f"gi.repository.{name}")
        # Return a do-nothing sentinel for any attribute access.
        stub.__getattr__ = lambda self, n: MagicMock()  # type: ignore[attr-defined]
        return MagicMock()

    repo_mod.Gtk = _make_stub("Gtk")
    repo_mod.Adw = _make_stub("Adw")
    repo_mod.GLib = _make_stub("GLib")

    gi_mod.repository = repo_mod  # type: ignore[attr-defined]

    sys.modules["gi"] = gi_mod
    sys.modules["gi.repository"] = repo_mod
    sys.modules["gi.repository.Gtk"] = repo_mod.Gtk
    sys.modules["gi.repository.Adw"] = repo_mod.Adw
    sys.modules["gi.repository.GLib"] = repo_mod.GLib


_install_gi_stub()

# Now safe to import the pure helpers.
from hermes.shell.presentation.gtk4.widgets.tasks_view import (  # noqa: E402
    _fmt_datetime,
    _fmt_next_run,
)


# ---------------------------------------------------------------------------
# _fmt_datetime
# ---------------------------------------------------------------------------


class TestFmtDatetime:
    def test_none_returns_dash(self) -> None:
        assert _fmt_datetime(None) == "—"

    def test_empty_string_returns_dash(self) -> None:
        assert _fmt_datetime("") == "—"

    def test_today_shows_hoy_prefix(self) -> None:
        # A timestamp very close to now should produce "hoy HH:MM".
        from datetime import UTC, datetime, timedelta

        recent = (datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat()
        result = _fmt_datetime(recent)
        assert result.startswith("hoy ")

    def test_yesterday_shows_ayer_prefix(self) -> None:
        from datetime import UTC, datetime, timedelta

        yesterday = (datetime.now(tz=UTC) - timedelta(hours=25)).isoformat()
        result = _fmt_datetime(yesterday)
        assert result.startswith("ayer ")

    def test_older_date_shows_full_date(self) -> None:
        result = _fmt_datetime("2025-01-15T09:30:00+00:00")
        assert "2025-01-15" in result

    def test_invalid_iso_returns_input(self) -> None:
        result = _fmt_datetime("not-a-date")
        assert result == "not-a-date"

    def test_naive_datetime_handled(self) -> None:
        # ISO without timezone offset — must not crash.
        result = _fmt_datetime("2026-05-01T09:00:00")
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# _fmt_next_run
# ---------------------------------------------------------------------------


class TestFmtNextRun:
    def test_none_returns_dash(self) -> None:
        assert _fmt_next_run(None) == "—"

    def test_empty_string_returns_dash(self) -> None:
        assert _fmt_next_run("") == "—"

    def test_within_an_hour_shows_en_minutes(self) -> None:
        from datetime import UTC, datetime, timedelta

        soon = (datetime.now(tz=UTC) + timedelta(minutes=20)).isoformat()
        result = _fmt_next_run(soon)
        assert "min" in result

    def test_within_a_day_shows_hours(self) -> None:
        from datetime import UTC, datetime, timedelta

        later = (datetime.now(tz=UTC) + timedelta(hours=3)).isoformat()
        result = _fmt_next_run(later)
        assert "h" in result

    def test_tomorrow_shows_manana_prefix(self) -> None:
        from datetime import UTC, datetime, timedelta

        tomorrow = (datetime.now(tz=UTC) + timedelta(hours=26)).isoformat()
        result = _fmt_next_run(tomorrow)
        assert result.startswith("mañana ")

    def test_past_timestamp_shows_full_date(self) -> None:
        result = _fmt_next_run("2025-01-01T09:00:00+00:00")
        assert "2025-01-01" in result

    def test_invalid_returns_input(self) -> None:
        result = _fmt_next_run("garbage")
        assert result == "garbage"
