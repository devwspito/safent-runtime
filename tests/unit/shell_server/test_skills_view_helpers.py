"""Unit tests for headless-safe helpers in skills_view.py.

The GTK4 widget itself cannot be instantiated in a headless CI environment,
but the pure helpers are side-effect-free and fully testable without a display.

Pattern mirrors test_tasks_view_helpers.py: gi is stubbed before import so the
module loads cleanly in any CI environment without a running display.
"""

from __future__ import annotations

import sys
import types
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

    gi_mod.repository = repo_mod  # type: ignore[attr-defined]

    sys.modules["gi"] = gi_mod
    sys.modules["gi.repository"] = repo_mod
    sys.modules["gi.repository.Gtk"] = repo_mod.Gtk
    sys.modules["gi.repository.Adw"] = repo_mod.Adw
    sys.modules["gi.repository.GLib"] = repo_mod.GLib


_install_gi_stub()

from hermes.shell.presentation.gtk4.widgets.skills_view import (  # noqa: E402
    _composio_badge_label,
    _connected_account_display_name,
    _site_id_from_input,
    _site_url_from_input,
)


# ---------------------------------------------------------------------------
# _site_id_from_input
# ---------------------------------------------------------------------------


class TestSiteIdFromInput:
    def test_empty_returns_empty(self) -> None:
        assert _site_id_from_input("") == ""

    def test_whitespace_returns_empty(self) -> None:
        assert _site_id_from_input("   ") == ""

    def test_strips_https(self) -> None:
        assert _site_id_from_input("https://amazon.es/dp/123") == "amazon.es"

    def test_strips_http(self) -> None:
        assert _site_id_from_input("http://example.com/path") == "example.com"

    def test_bare_domain(self) -> None:
        assert _site_id_from_input("amazon.es") == "amazon.es"

    def test_lowercased(self) -> None:
        assert _site_id_from_input("https://EXAMPLE.COM") == "example.com"


# ---------------------------------------------------------------------------
# _site_url_from_input
# ---------------------------------------------------------------------------


class TestSiteUrlFromInput:
    def test_empty_returns_empty(self) -> None:
        assert _site_url_from_input("") == ""

    def test_https_preserved(self) -> None:
        assert _site_url_from_input("https://example.com") == "https://example.com"

    def test_http_preserved(self) -> None:
        assert _site_url_from_input("http://example.com") == "http://example.com"

    def test_bare_domain_gets_https(self) -> None:
        assert _site_url_from_input("example.com") == "https://example.com"

    def test_strips_surrounding_whitespace(self) -> None:
        result = _site_url_from_input("  example.com  ")
        assert result == "https://example.com"


# ---------------------------------------------------------------------------
# _composio_badge_label
# ---------------------------------------------------------------------------


class TestComposioBadgeLabel:
    def test_no_kind_field_returns_none(self) -> None:
        assert _composio_badge_label({}) is None

    def test_recording_kind_returns_none(self) -> None:
        assert _composio_badge_label({"skill_kind": "recording"}) is None

    def test_composio_kind_without_toolkit_returns_generic(self) -> None:
        result = _composio_badge_label({"skill_kind": "composio"})
        assert result == "Integración"

    def test_composio_kind_with_toolkit(self) -> None:
        result = _composio_badge_label({"skill_kind": "composio", "toolkit": "GMAIL"})
        assert result == "Integración: Gmail"

    def test_composio_kind_with_composio_toolkit_field(self) -> None:
        result = _composio_badge_label(
            {"skill_kind": "composio", "composio_toolkit": "GOOGLE_CALENDAR"}
        )
        assert result == "Integración: Google Calendar"

    def test_toolkit_preferred_over_composio_toolkit(self) -> None:
        result = _composio_badge_label(
            {"skill_kind": "composio", "toolkit": "OUTLOOK", "composio_toolkit": "OTHER"}
        )
        assert result == "Integración: Outlook"

    def test_falls_back_to_kind_field(self) -> None:
        # Some DTOs may use "kind" instead of "skill_kind".
        result = _composio_badge_label({"kind": "composio", "toolkit": "SLACK"})
        assert result == "Integración: Slack"

    def test_empty_toolkit_falls_back_to_generic(self) -> None:
        result = _composio_badge_label({"skill_kind": "composio", "toolkit": ""})
        assert result == "Integración"

    def test_underscore_in_toolkit_becomes_space(self) -> None:
        result = _composio_badge_label({"skill_kind": "composio", "toolkit": "GOOGLE_DRIVE"})
        assert result == "Integración: Google Drive"


# ---------------------------------------------------------------------------
# _connected_account_display_name
# ---------------------------------------------------------------------------


class TestConnectedAccountDisplayName:
    def test_empty_dict_returns_placeholder(self) -> None:
        assert _connected_account_display_name({}) == "(sin nombre)"

    def test_name_field_preferred(self) -> None:
        acc = {"name": "My Gmail", "toolkit_slug": "GMAIL"}
        assert _connected_account_display_name(acc) == "My Gmail"

    def test_toolkit_slug_fallback(self) -> None:
        acc = {"toolkit_slug": "GMAIL"}
        assert _connected_account_display_name(acc) == "Gmail"

    def test_app_name_fallback(self) -> None:
        acc = {"app_name": "OUTLOOK"}
        assert _connected_account_display_name(acc) == "Outlook"

    def test_underscores_replaced_and_title_cased(self) -> None:
        acc = {"toolkit_slug": "GOOGLE_CALENDAR"}
        assert _connected_account_display_name(acc) == "Google Calendar"
