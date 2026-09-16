"""Regression (025 Top-3): egress deny-by-default on fresh installs.

SECURITY.md promises the browser/terminal plane is default-deny, but the live
matrix (specs/025-safent-repaso/matriz-en-vivo.md §4) showed `GET /egress/mode`
resolving to ``allow`` on a fresh install — `example.com`/`example.org` reachable
with zero owner grants. Root cause: `_DEFAULT_NETWORK_MODE` (used only when no
`egress-mode.json` exists yet, i.e. a fresh install) was `_ALLOW_MODE`.

These tests pin:
  - an UNSET mode (no persisted file) resolves to "deny";
  - an EXISTING install with an explicit "allow" (or "deny") on disk keeps it —
    the default only kicks in when the value is truly absent;
  - the payload pushed to the proxy control socket for a fresh install is the
    default-deny shape (mode="default-deny", empty domains) — this is the
    "generated proxy config" for the unset case.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server import egress_api
from hermes.shell_server.egress_api import create_egress_router

pytestmark = pytest.mark.unit


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setattr(egress_api, "_MODE_PATH", tmp_path / "egress-mode.json")
    monkeypatch.setattr(egress_api, "_GRANTS_PATH", tmp_path / "egress-grants.json")
    monkeypatch.setattr(egress_api, "_DENY_PATH", tmp_path / "egress-denylist.json")
    monkeypatch.setattr(egress_api, "_MCP_GRANTS_PATH", tmp_path / "mcp-egress-grants.json")
    # No proxy control socket in the unit sandbox — pushes fail-soft to False,
    # which is fine: these tests assert on the *resolved mode*, not the push.
    monkeypatch.setattr(egress_api, "_PROXY_SOCK", str(tmp_path / "no-such.sock"))
    app = FastAPI()
    app.include_router(create_egress_router())
    return TestClient(app)


class TestDefaultModeResolution:
    def test_unset_mode_resolves_to_deny(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(egress_api, "_MODE_PATH", tmp_path / "egress-mode.json")
        assert not (tmp_path / "egress-mode.json").exists()
        assert egress_api._load_mode() == "deny"

    def test_fresh_install_get_mode_is_deny(self, client: TestClient) -> None:
        r = client.get("/api/v1/egress/mode")
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "deny"
        assert "only explicitly allowed domains" in body["description"]

    def test_existing_install_explicit_allow_is_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An install that already set mode=allow keeps it — the new default
        must NOT silently flip existing installs (only the unset case changes)."""
        mode_path = tmp_path / "egress-mode.json"
        mode_path.write_text('{"mode": "allow"}')
        monkeypatch.setattr(egress_api, "_MODE_PATH", mode_path)
        assert egress_api._load_mode() == "allow"

    def test_existing_install_explicit_deny_is_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mode_path = tmp_path / "egress-mode.json"
        mode_path.write_text('{"mode": "deny"}')
        monkeypatch.setattr(egress_api, "_MODE_PATH", mode_path)
        assert egress_api._load_mode() == "deny"

    def test_fresh_install_pushes_default_deny_proxy_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The generated proxy-control payload for a fresh (unset) install must
        be default-deny with no domains granted yet — not open-logged."""
        monkeypatch.setattr(egress_api, "_MODE_PATH", tmp_path / "egress-mode.json")
        monkeypatch.setattr(egress_api, "_GRANTS_PATH", tmp_path / "egress-grants.json")

        pushed: dict = {}

        def _fake_push_session(session_id, domains, mode="default-deny", deny=None):
            pushed.update(session_id=session_id, domains=domains, mode=mode, deny=deny)
            return True

        monkeypatch.setattr(egress_api, "_push_session", _fake_push_session)

        ok = egress_api._apply_network_mode()

        assert ok is True
        assert pushed["mode"] == "default-deny"
        assert pushed["domains"] == []
