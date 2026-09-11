"""Unit tests for the egress domain-validation error shape (spec 025 hallazgo #5).

Before the fix, `POST /deny/add`, `POST /domains/grant`, and
`POST /mcp/domains/grant` returned HTTP 200 with `{"ok": false, "error": ...}`
on an invalid domain — a client that only checks the HTTP status code (curl,
a script, a future frontend view) would believe the domain was
granted/denied when nothing happened. The sibling `POST /mode` endpoint in
the SAME file already used `HTTPException(422, detail={"code", "message"})`
for its own validation error — the owner's standing rule for the whole API
— so the inconsistency was internal to this one file.

These 3 tests only exercise the validation short-circuit (an invalid domain
fails `_DOMAIN_RE` before any persistence or proxy-socket I/O happens), so
they are fully hermetic: no filesystem writes under /var/lib/hermes, no
UNIX socket connect.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server import egress_api
from hermes.shell_server.egress_api import create_egress_router

pytestmark = pytest.mark.unit

_INVALID_DOMAIN = "not a domain!!"


def _client() -> TestClient:
    app = FastAPI()
    app.state.shell_webui_token = "owner-ui"
    app.include_router(create_egress_router())
    return TestClient(app, headers={"Authorization": "Bearer owner-ui"})


@pytest.mark.parametrize("path", [
    "mode", "deny/add", "deny/remove", "domains/grant", "domains/revoke",
    "mcp/domains/grant", "mcp/domains/revoke",
])
@pytest.mark.parametrize("token", ["", "internal-daemon", "another-owner"])
def test_non_owner_cannot_change_any_egress_rule(
    path: str, token: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("_MODE_PATH", "_GRANTS_PATH", "_DENY_PATH", "_MCP_GRANTS_PATH"):
        monkeypatch.setattr(egress_api, name, tmp_path / name)
    client = _client()
    client.headers["Authorization"] = f"Bearer {token}"
    payload = {"mode": "allow"} if path == "mode" else {"domain": "example.com"}
    response = client.post(f"/api/v1/egress/{path}", json=payload)
    assert response.status_code == 403
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/egress/deny/add",
        "/api/v1/egress/domains/grant",
        "/api/v1/egress/mcp/domains/grant",
    ],
)
class TestInvalidDomainReturns422NotOkFalse:
    def test_status_code_is_422(self, path: str) -> None:
        r = _client().post(path, json={"domain": _INVALID_DOMAIN})
        assert r.status_code == 422

    def test_body_matches_the_rest_of_the_api_shape(self, path: str) -> None:
        """Same {"detail": {"code", "message"}} shape as POST /mode's own 422
        (egress_api.py's own set_mode) — never {"ok": false} with HTTP 200."""
        r = _client().post(path, json={"domain": _INVALID_DOMAIN})
        body = r.json()
        assert "ok" not in body
        assert body["detail"]["code"] == "invalid_domain"
        assert _INVALID_DOMAIN in body["detail"]["message"]

    def test_a_status_only_client_cannot_mistake_this_for_success(self, path: str) -> None:
        """A client that only checks 2xx (the exact failure mode this fixes)
        must see a NON-2xx status for an invalid domain."""
        r = _client().post(path, json={"domain": _INVALID_DOMAIN})
        assert not (200 <= r.status_code < 300)
