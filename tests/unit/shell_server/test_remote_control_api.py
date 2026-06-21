"""Tests REST endpoints + token redemption single-use + binding."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.remote_control.api import (
    create_remote_control_router,
)

pytestmark = pytest.mark.unit


def _payload(*, approved: bool = True, ttl: int = 900) -> dict:
    return {
        "node_installation_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "operator_id": str(uuid4()),
        "scope": "os_full_desktop",
        "dtls_fingerprint": "a" * 64,
        "consent_id": str(uuid4()),
        "local_operator_approved": approved,
        "ttl_seconds": ttl,
    }


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_remote_control_router(
            db_path=tmp_path / "rc.db",
            cipher_key=os.urandom(32),
            cipher_kid="test-kid",
            signaling_ws_base="ws://127.0.0.1:7518/rc",
        )
    )
    return TestClient(app)


class TestIssue:
    def test_issue_returns_session_and_redeem_url(self, client: TestClient) -> None:
        r = client.post("/api/v1/remote-control/sessions", json=_payload())
        assert r.status_code == 201
        body = r.json()
        assert body["state"] == "issued"
        assert body["redeem_url"].startswith("/api/v1/remote-control/redeem/")
        assert body["signaling_ws_url"].endswith(body["session_id"])

    def test_issue_without_approval_returns_403(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/remote-control/sessions",
            json=_payload(approved=False),
        )
        assert r.status_code == 403

    def test_ttl_too_long_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/remote-control/sessions",
            json=_payload(ttl=10000),
        )
        # 10000 > 3600 — pydantic catches it.
        assert r.status_code == 422


class TestList:
    def test_list_after_issue(self, client: TestClient) -> None:
        client.post("/api/v1/remote-control/sessions", json=_payload())
        items = client.get("/api/v1/remote-control/sessions").json()
        assert len(items) >= 1
        assert items[0]["state"] == "issued"


class TestRevoke:
    def test_revoke_transitions_to_ended(self, client: TestClient) -> None:
        r = client.post("/api/v1/remote-control/sessions", json=_payload())
        sid = r.json()["session_id"]
        rev = client.post(f"/api/v1/remote-control/sessions/{sid}/revoke")
        assert rev.status_code == 200
        body = rev.json()
        assert body["state"] == "ended"
        assert body["end_reason"] == "local_operator_ended"

    def test_revoke_unknown_returns_404(self, client: TestClient) -> None:
        r = client.post(
            f"/api/v1/remote-control/sessions/{uuid4()}/revoke"
        )
        assert r.status_code == 404


class TestRedeem:
    def test_single_use_redeem(self, client: TestClient) -> None:
        r = client.post("/api/v1/remote-control/sessions", json=_payload())
        redeem_url = r.json()["redeem_url"]
        first = client.get(redeem_url, follow_redirects=False)
        assert first.status_code == 302
        assert "viewer.html" in first.headers["location"]
        # Cookie must be HttpOnly + Secure + SameSite=Strict.
        cookie_hdr = first.headers["set-cookie"].lower()
        assert "httponly" in cookie_hdr
        assert "samesite=strict" in cookie_hdr

        # Second redemption rejected.
        second = client.get(redeem_url, follow_redirects=False)
        assert second.status_code == 410

    def test_redeem_invalid_token(self, client: TestClient) -> None:
        r = client.get(
            "/api/v1/remote-control/redeem/nonexistent",
            follow_redirects=False,
        )
        assert r.status_code == 410

    def test_cookie_max_age_is_integer_seconds(self, client: TestClient) -> None:
        # Regresión: max_age debe ser segundos int, no un timedelta crudo
        # ("Max-Age=0:14:59.88" rompe el cookie).
        r = client.post("/api/v1/remote-control/sessions", json=_payload(ttl=900))
        redeem_url = r.json()["redeem_url"]
        resp = client.get(redeem_url, follow_redirects=False)
        cookie = resp.headers["set-cookie"]
        import re

        m = re.search(r"Max-Age=([^;]+)", cookie, re.IGNORECASE)
        assert m is not None
        # Debe parsear como int (no "0:14:59.88").
        assert m.group(1).strip().isdigit()
        assert 0 < int(m.group(1)) <= 900


class TestDTOExposesBinding:
    def test_session_dto_includes_dtls_fingerprint(
        self, client: TestClient
    ) -> None:
        # Regresión: el daemon de signaling consume dtls_fingerprint del DTO.
        fp = "f" * 64
        payload = _payload()
        payload["dtls_fingerprint"] = fp
        r = client.post("/api/v1/remote-control/sessions", json=payload)
        sid = r.json()["session_id"]
        dto = client.get(f"/api/v1/remote-control/sessions/{sid}").json()
        assert dto["dtls_fingerprint"] == fp


class TestBindingViolation:
    def test_binding_violation_transitions_ended(self, client: TestClient) -> None:
        r = client.post("/api/v1/remote-control/sessions", json=_payload())
        sid = r.json()["session_id"]
        bv = client.post(
            f"/api/v1/remote-control/sessions/{sid}/binding-violation"
        )
        assert bv.status_code == 200
        assert bv.json()["state"] == "ended"
        assert bv.json()["end_reason"] == "binding_violated"
