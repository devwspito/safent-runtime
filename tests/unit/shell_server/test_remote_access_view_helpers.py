"""Unit tests for the pure helper in remote_access_view.py.

The GTK4 widget itself cannot be instantiated in a headless CI environment,
but _read_remote_url is a side-effect-free file-reader with no GTK
dependency and is fully testable without a display.

Pattern mirrors test_tasks_view_helpers.py and test_skills_view_helpers.py:
gi is stubbed before the module is imported so the suite stays headless-safe.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Headless guard — stub out gi/GTK before the module is imported.
# ---------------------------------------------------------------------------


def _install_gi_stub() -> None:
    if "gi" in sys.modules:
        return

    gi_mod = types.ModuleType("gi")
    gi_mod.require_version = lambda *a, **kw: None  # type: ignore[attr-defined]

    repo_mod = types.ModuleType("gi.repository")
    repo_mod.Gtk = MagicMock()
    repo_mod.Adw = MagicMock()
    repo_mod.GLib = MagicMock()
    repo_mod.Gdk = MagicMock()

    gi_mod.repository = repo_mod  # type: ignore[attr-defined]

    sys.modules["gi"] = gi_mod
    sys.modules["gi.repository"] = repo_mod
    sys.modules["gi.repository.Gtk"] = repo_mod.Gtk
    sys.modules["gi.repository.Adw"] = repo_mod.Adw
    sys.modules["gi.repository.GLib"] = repo_mod.GLib
    sys.modules["gi.repository.Gdk"] = repo_mod.Gdk


_install_gi_stub()

from hermes.shell.presentation.gtk4.widgets.remote_access_view import (  # noqa: E402
    _parse_active,
    _read_remote_url,
)


# ---------------------------------------------------------------------------
# _read_remote_url
# ---------------------------------------------------------------------------


class TestReadRemoteUrl:
    def test_returns_url_from_existing_file(self, tmp_path: Path) -> None:
        url = "https://example-tunnel.trycloudflare.com/vnc.html?autoconnect=true"
        f = tmp_path / "remote-url"
        f.write_text(url, encoding="utf-8")

        result = _read_remote_url(f)

        assert result == url

    def test_strips_trailing_newline(self, tmp_path: Path) -> None:
        url = "https://example.trycloudflare.com/vnc.html"
        f = tmp_path / "remote-url"
        f.write_text(url + "\n", encoding="utf-8")

        assert _read_remote_url(f) == url

    def test_strips_surrounding_whitespace(self, tmp_path: Path) -> None:
        url = "https://example.trycloudflare.com/vnc.html"
        f = tmp_path / "remote-url"
        f.write_text(f"  {url}  \n", encoding="utf-8")

        assert _read_remote_url(f) == url

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"

        assert _read_remote_url(missing) is None

    def test_returns_none_for_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "remote-url"
        f.write_text("", encoding="utf-8")

        assert _read_remote_url(f) is None

    def test_returns_none_for_whitespace_only_file(self, tmp_path: Path) -> None:
        f = tmp_path / "remote-url"
        f.write_text("   \n\n  ", encoding="utf-8")

        assert _read_remote_url(f) is None

    def test_full_cloudflare_url_with_query_preserved(self, tmp_path: Path) -> None:
        url = (
            "https://thereof-sugar-demonstration-costume.trycloudflare.com"
            "/vnc.html?autoconnect=true&resize=scale&reconnect=true&password=HermesRemote2026"
        )
        f = tmp_path / "remote-url"
        f.write_text(url, encoding="utf-8")

        assert _read_remote_url(f) == url


# ---------------------------------------------------------------------------
# _parse_active
# ---------------------------------------------------------------------------


class TestParseActive:
    def test_true_when_active_true(self) -> None:
        assert _parse_active({"active": True}) is True

    def test_false_when_active_false(self) -> None:
        assert _parse_active({"active": False}) is False

    def test_false_when_key_missing(self) -> None:
        assert _parse_active({}) is False

    def test_false_when_none_value(self) -> None:
        assert _parse_active({"active": None}) is False
