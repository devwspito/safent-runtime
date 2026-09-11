"""Tests for the governed tailnet control API (spec 022).

Coverage:
  GET /tailnet — unconfigured (no status.json) → all-empty/false response
  GET /tailnet — configured → maps status.json fields, drops malformed peers
  GET /tailnet/peers — mirrors the peers list from status.json
  POST /connect — invalid auth-key shape → 422, nothing persisted/staged
  POST /connect — valid key → 202, vault.encrypt called, staged file has
                  action=connect + the plaintext key, key NEVER in the response
  POST /connect — vault write failure → 503
  POST /disconnect — valid password → 200, staged file has action=disconnect
  POST /disconnect — invalid chars → 400, nothing staged
  POST /disconnect — rate limited after N failures → 429
  POST /disconnect — response never contains the password
  Staged files are 0600
  GET /ssh-hosts — lists approved hosts + approved_at (spec 022 v2)
  DELETE /ssh-hosts/{host} — requires owner TOTP; not-enrolled → 403; wrong
                             code → 401; correct code → revokes + 200
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.remote_access_tunnel.rate_limiter import PasswordRateLimiter
from hermes.shell_server.tailnet.api import create_tailnet_router
from hermes.tailnet_ssh.infrastructure.json_host_allowlist_store import JsonHostAllowlistStore

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def status_path(tmp_path: Path) -> Path:
    return tmp_path / "run" / "status.json"


@pytest.fixture
def control_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run" / "tailscale-control"
    d.mkdir(mode=0o700, parents=True)
    return d


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    return tmp_path / "var-lib" / "tailscale-authkey.enc"


@pytest.fixture
def fake_vault() -> MagicMock:
    vault = MagicMock()
    vault.encrypt.return_value = b"\x00" * 12 + b"ciphertext-blob"
    return vault


@pytest.fixture
def fresh_limiter() -> PasswordRateLimiter:
    return PasswordRateLimiter()


@pytest.fixture
def client(
    status_path: Path,
    control_dir: Path,
    vault_path: Path,
    fake_vault: MagicMock,
    fresh_limiter: PasswordRateLimiter,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_tailnet_router(
            vault=fake_vault,
            status_path=status_path,
            control_dir=control_dir,
            vault_path=vault_path,
            rate_limiter=fresh_limiter,
        )
    )
    return TestClient(app)


def _staged(control_dir: Path) -> Path:
    return control_dir / "request.json"


VALID_AUTH_KEY = "tskey-auth-kABC123-defghijklmnopqrstuvwxyz012345"


# ---------------------------------------------------------------------------
# GET /tailnet
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_unconfigured_when_status_file_absent(self, client: TestClient) -> None:
        r = client.get("/api/v1/tailnet")
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "configured": False,
            "online": False,
            "node_name": None,
            "magicdns_suffix": None,
            "tailnet": None,
            "peers": [],
            "last_attempt": None,
        }

    def test_configured_maps_status_fields(
        self, client: TestClient, status_path: Path
    ) -> None:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps(
                {
                    "node_name": "safent-agent",
                    "magicdns_suffix": "tail1234.ts.net",
                    "tailnet": "acme.ts.net",
                    "online": True,
                    "peers": [
                        {"name": "laptop", "online": True},
                        {"name": "server", "online": False},
                    ],
                }
            )
        )
        r = client.get("/api/v1/tailnet")
        assert r.status_code == 200
        body = r.json()
        assert body["configured"] is True
        assert body["online"] is True
        assert body["node_name"] == "safent-agent"
        assert body["magicdns_suffix"] == "tail1234.ts.net"
        assert body["tailnet"] == "acme.ts.net"
        assert body["peers"] == [
            {"name": "laptop", "online": True},
            {"name": "server", "online": False},
        ]

    def test_malformed_peer_entries_are_dropped(
        self, client: TestClient, status_path: Path
    ) -> None:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps(
                {
                    "node_name": "safent-agent",
                    "magicdns_suffix": "tail1234.ts.net",
                    "tailnet": "acme.ts.net",
                    "online": True,
                    "peers": [
                        {"name": "laptop", "online": True},
                        {"online": True},  # missing name — dropped
                        "not-a-dict",  # dropped
                    ],
                }
            )
        )
        r = client.get("/api/v1/tailnet")
        assert r.json()["peers"] == [{"name": "laptop", "online": True}]

    def test_corrupt_json_treated_as_unconfigured(
        self, client: TestClient, status_path: Path
    ) -> None:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text("{not valid")
        r = client.get("/api/v1/tailnet")
        assert r.json()["configured"] is False

    def test_response_never_contains_secrets(
        self, client: TestClient, status_path: Path
    ) -> None:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps(
                {
                    "node_name": "safent-agent",
                    "magicdns_suffix": "tail1234.ts.net",
                    "tailnet": "acme.ts.net",
                    "online": True,
                    "peers": [],
                }
            )
        )
        r = client.get("/api/v1/tailnet")
        assert "auth_key" not in r.text
        assert "tskey" not in r.text


# ---------------------------------------------------------------------------
# Regression (025 hallazgo D): a rejected auth_key made `tailscale up` fail
# 5/5, yet status.json still existed (the watcher writes it as soon as
# tailscaled STARTS, independent of login outcome) — so `configured` used to
# read True. `configured` now means "logged in" (== online), and last_attempt
# lets the UI show "clave rechazada" instead of claiming success.
# ---------------------------------------------------------------------------


class TestLastAttemptAndConfiguredMeansLoggedIn:
    def test_status_json_exists_but_never_logged_in_is_not_configured(
        self, client: TestClient, status_path: Path,
    ) -> None:
        """Exactly the matrix R26 scenario: the watcher wrote a status
        document (tailscaled started) but online is false — a rejected key
        must NOT read as configured:true."""
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps({
                "node_name": "2846102aaf5a", "magicdns_suffix": "", "tailnet": "",
                "online": False, "peers": [],
                "last_attempt": {
                    "at": "2026-09-10T14:03:00+00:00", "ok": False, "error_kind": "tailscale_up_failed",
                },
            })
        )

        r = client.get("/api/v1/tailnet")

        body = r.json()
        assert body["configured"] is False
        assert body["online"] is False
        assert body["last_attempt"] == {
            "at": "2026-09-10T14:03:00+00:00", "ok": False, "error_kind": "tailscale_up_failed",
        }

    def test_successful_last_attempt_is_surfaced_alongside_online_true(
        self, client: TestClient, status_path: Path,
    ) -> None:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps({
                "node_name": "safent-agent", "magicdns_suffix": "tail1234.ts.net",
                "tailnet": "acme.ts.net", "online": True, "peers": [],
                "last_attempt": {"at": "2026-09-10T14:05:00+00:00", "ok": True, "error_kind": None},
            })
        )

        r = client.get("/api/v1/tailnet")

        body = r.json()
        assert body["configured"] is True
        assert body["last_attempt"]["ok"] is True

    def test_missing_last_attempt_is_null_not_an_error(
        self, client: TestClient, status_path: Path,
    ) -> None:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps({
                "node_name": "safent-agent", "magicdns_suffix": "tail1234.ts.net",
                "tailnet": "acme.ts.net", "online": True, "peers": [],
            })
        )

        r = client.get("/api/v1/tailnet")

        assert r.json()["last_attempt"] is None

    def test_malformed_last_attempt_is_dropped_not_surfaced(
        self, client: TestClient, status_path: Path,
    ) -> None:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps({
                "node_name": "safent-agent", "magicdns_suffix": "", "tailnet": "",
                "online": False, "peers": [],
                "last_attempt": {"ok": "not-a-bool"},  # missing 'at', wrong type
            })
        )

        r = client.get("/api/v1/tailnet")

        assert r.json()["last_attempt"] is None

    def test_last_attempt_response_never_contains_a_key(
        self, client: TestClient, status_path: Path,
    ) -> None:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps({
                "node_name": "safent-agent", "magicdns_suffix": "", "tailnet": "",
                "online": False, "peers": [],
                "last_attempt": {
                    "at": "2026-09-10T14:03:00+00:00", "ok": False,
                    "error_kind": "tailscale_up_failed",
                },
            })
        )

        r = client.get("/api/v1/tailnet")

        assert "tskey" not in r.text
        assert "auth_key" not in r.text


# ---------------------------------------------------------------------------
# GET /tailnet/peers
# ---------------------------------------------------------------------------


class TestGetPeers:
    def test_mirrors_status_peers(self, client: TestClient, status_path: Path) -> None:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps({"peers": [{"name": "laptop", "online": True}]})
        )
        r = client.get("/api/v1/tailnet/peers")
        assert r.status_code == 200
        assert r.json() == {"peers": [{"name": "laptop", "online": True}]}

    def test_empty_when_unconfigured(self, client: TestClient) -> None:
        r = client.get("/api/v1/tailnet/peers")
        assert r.json() == {"peers": []}


# ---------------------------------------------------------------------------
# POST /connect
# ---------------------------------------------------------------------------


class TestConnectEndpoint:
    def test_invalid_shape_returns_422(
        self, client: TestClient, control_dir: Path, fake_vault: MagicMock
    ) -> None:
        r = client.post("/api/v1/tailnet/connect", json={"auth_key": "not-a-real-key"})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "invalid_auth_key"
        assert not _staged(control_dir).exists()
        fake_vault.encrypt.assert_not_called()

    def test_valid_key_returns_202(self, client: TestClient) -> None:
        r = client.post("/api/v1/tailnet/connect", json={"auth_key": VALID_AUTH_KEY})
        assert r.status_code == 202
        assert r.json() == {"staged": True}

    def test_valid_key_persists_via_vault(
        self, client: TestClient, fake_vault: MagicMock, vault_path: Path
    ) -> None:
        client.post("/api/v1/tailnet/connect", json={"auth_key": VALID_AUTH_KEY})
        fake_vault.encrypt.assert_called_once()
        _, kwargs = fake_vault.encrypt.call_args
        assert kwargs["plaintext"] == VALID_AUTH_KEY
        assert vault_path.exists()
        assert stat.S_IMODE(vault_path.stat().st_mode) == 0o600

    def test_valid_key_stages_control_request(
        self, client: TestClient, control_dir: Path
    ) -> None:
        client.post("/api/v1/tailnet/connect", json={"auth_key": VALID_AUTH_KEY})
        data = json.loads(_staged(control_dir).read_text())
        assert data["action"] == "connect"
        assert data["auth_key"] == VALID_AUTH_KEY
        assert "requested_at" in data

    def test_staged_file_permissions_0600(
        self, client: TestClient, control_dir: Path
    ) -> None:
        client.post("/api/v1/tailnet/connect", json={"auth_key": VALID_AUTH_KEY})
        mode = _staged(control_dir).stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_response_never_echoes_the_key(self, client: TestClient) -> None:
        r = client.post("/api/v1/tailnet/connect", json={"auth_key": VALID_AUTH_KEY})
        assert VALID_AUTH_KEY not in r.text

    def test_vault_write_failure_returns_503(
        self, client: TestClient, fake_vault: MagicMock
    ) -> None:
        fake_vault.encrypt.side_effect = OSError("disk full")
        r = client.post("/api/v1/tailnet/connect", json={"auth_key": VALID_AUTH_KEY})
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "vault_write_failed"

    def test_empty_key_returns_422(self, client: TestClient) -> None:
        r = client.post("/api/v1/tailnet/connect", json={"auth_key": ""})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /disconnect
# ---------------------------------------------------------------------------


class TestDisconnectEndpoint:
    def test_valid_password_returns_200_and_stages(
        self, client: TestClient, control_dir: Path
    ) -> None:
        r = client.post(
            "/api/v1/tailnet/disconnect", json={"password": "validpassword123"}
        )
        assert r.status_code == 200
        assert r.json() == {"staged": True}
        data = json.loads(_staged(control_dir).read_text())
        assert data["action"] == "disconnect"
        assert data["password"] == "validpassword123"

    def test_staged_file_permissions_0600(
        self, client: TestClient, control_dir: Path
    ) -> None:
        client.post("/api/v1/tailnet/disconnect", json={"password": "validpassword123"})
        mode = _staged(control_dir).stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_response_never_contains_password(self, client: TestClient) -> None:
        secret = "mysecretpassword99"
        r = client.post("/api/v1/tailnet/disconnect", json={"password": secret})
        assert secret not in r.text

    def test_invalid_chars_returns_400(
        self, client: TestClient, control_dir: Path
    ) -> None:
        r = client.post(
            "/api/v1/tailnet/disconnect", json={"password": "validpwd\x00evil"}
        )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "invalid_password"
        assert not _staged(control_dir).exists()

    def test_too_short_returns_422(self, client: TestClient) -> None:
        r = client.post("/api/v1/tailnet/disconnect", json={"password": "short"})
        assert r.status_code == 422

    def test_rate_limited_after_max_failures(
        self, control_dir: Path, status_path: Path, vault_path: Path, fake_vault: MagicMock
    ) -> None:
        limiter = PasswordRateLimiter(max_failures=5, window_seconds=60)
        app = FastAPI()
        app.include_router(
            create_tailnet_router(
                vault=fake_vault,
                status_path=status_path,
                control_dir=control_dir,
                vault_path=vault_path,
                rate_limiter=limiter,
            )
        )
        c = TestClient(app, raise_server_exceptions=False)
        for _ in range(5):
            c.post("/api/v1/tailnet/disconnect", json={"password": "validpassword1"})
        r = c.post("/api/v1/tailnet/disconnect", json={"password": "validpassword2"})
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "too_many_attempts"


# ---------------------------------------------------------------------------
# GET /ssh-hosts, DELETE /ssh-hosts/{host} (spec 022 v2)
# ---------------------------------------------------------------------------


@pytest.fixture
def ssh_allowlist_path(tmp_path: Path) -> Path:
    return tmp_path / "run" / "ssh-allowlist.json"


@pytest.fixture
def owner_token() -> str:
    return "owner-ui"


def _ssh_client(
    *,
    status_path: Path,
    control_dir: Path,
    vault_path: Path,
    fake_vault: MagicMock,
    fresh_limiter: PasswordRateLimiter,
    ssh_allowlist_path: Path,
    owner_token: str,
) -> TestClient:
    app = FastAPI()
    app.state.shell_webui_token = "owner-ui"
    app.include_router(
        create_tailnet_router(
            vault=fake_vault,
            status_path=status_path,
            control_dir=control_dir,
            vault_path=vault_path,
            rate_limiter=fresh_limiter,
            ssh_allowlist_path=ssh_allowlist_path,
        )
    )
    return TestClient(app, headers={"Authorization": f"Bearer {owner_token}"})


class TestListSshHosts:
    def test_empty_when_no_hosts_approved(
        self, status_path, control_dir, vault_path, fake_vault, fresh_limiter,
        ssh_allowlist_path, owner_token,
    ) -> None:
        client = _ssh_client(
            status_path=status_path, control_dir=control_dir, vault_path=vault_path,
            fake_vault=fake_vault, fresh_limiter=fresh_limiter,
            ssh_allowlist_path=ssh_allowlist_path, owner_token=owner_token,
        )
        r = client.get("/api/v1/tailnet/ssh-hosts")
        assert r.status_code == 200
        assert r.json() == {"hosts": []}

    def test_lists_approved_hosts_with_approved_at(
        self, status_path, control_dir, vault_path, fake_vault, fresh_limiter,
        ssh_allowlist_path, owner_token,
    ) -> None:
        JsonHostAllowlistStore(ssh_allowlist_path).allow("db1.tailxxxx.ts.net")
        client = _ssh_client(
            status_path=status_path, control_dir=control_dir, vault_path=vault_path,
            fake_vault=fake_vault, fresh_limiter=fresh_limiter,
            ssh_allowlist_path=ssh_allowlist_path, owner_token=owner_token,
        )

        r = client.get("/api/v1/tailnet/ssh-hosts")

        assert r.status_code == 200
        hosts = r.json()["hosts"]
        assert len(hosts) == 1
        assert hosts[0]["host"] == "db1.tailxxxx.ts.net"
        assert hosts[0]["approved_at"]  # non-empty ISO-8601 string

    def test_requires_no_auth_header_itself_at_router_level(
        self, status_path, control_dir, vault_path, fake_vault, fresh_limiter,
        ssh_allowlist_path, owner_token,
    ) -> None:
        """This router mounted standalone has no bearer dependency — the
        production app (main.py) applies the global /api/v1/* bearer
        middleware around EVERY router, this one included; see
        TestUnauthenticatedRequiresBearerInProductionApp below for the
        full-app 401 check."""
        client = _ssh_client(
            status_path=status_path, control_dir=control_dir, vault_path=vault_path,
            fake_vault=fake_vault, fresh_limiter=fresh_limiter,
            ssh_allowlist_path=ssh_allowlist_path, owner_token=owner_token,
        )
        assert client.get("/api/v1/tailnet/ssh-hosts").status_code == 200


class TestRevokeSshHost:
    def test_missing_owner_session_returns_403(
        self, status_path, control_dir, vault_path, fake_vault, fresh_limiter,
        ssh_allowlist_path, owner_token,
    ) -> None:
        JsonHostAllowlistStore(ssh_allowlist_path).allow("db1.tailxxxx.ts.net")
        client = _ssh_client(
            status_path=status_path, control_dir=control_dir, vault_path=vault_path,
            fake_vault=fake_vault, fresh_limiter=fresh_limiter,
            ssh_allowlist_path=ssh_allowlist_path, owner_token=owner_token,
        )

        client.headers.clear()
        r = client.request(
            "DELETE", "/api/v1/tailnet/ssh-hosts/db1.tailxxxx.ts.net",
        )

        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "owner_session_required"
        assert JsonHostAllowlistStore(ssh_allowlist_path).is_allowed("db1.tailxxxx.ts.net") is True

    def test_internal_daemon_token_cannot_revoke(
        self, status_path, control_dir, vault_path, fake_vault, fresh_limiter,
        ssh_allowlist_path, owner_token,
    ) -> None:
        JsonHostAllowlistStore(ssh_allowlist_path).allow("db1.tailxxxx.ts.net")
        client = _ssh_client(
            status_path=status_path, control_dir=control_dir, vault_path=vault_path,
            fake_vault=fake_vault, fresh_limiter=fresh_limiter,
            ssh_allowlist_path=ssh_allowlist_path, owner_token=owner_token,
        )

        client.headers["Authorization"] = "Bearer internal-daemon"
        r = client.request(
            "DELETE", "/api/v1/tailnet/ssh-hosts/db1.tailxxxx.ts.net",
        )

        assert r.status_code == 403
        assert JsonHostAllowlistStore(ssh_allowlist_path).is_allowed("db1.tailxxxx.ts.net") is True

    def test_owner_revokes_without_mfa_and_returns_remaining_hosts(
        self, status_path, control_dir, vault_path, fake_vault, fresh_limiter,
        ssh_allowlist_path, owner_token,
    ) -> None:
        store = JsonHostAllowlistStore(ssh_allowlist_path)
        store.allow("db1.tailxxxx.ts.net")
        store.allow("build-box.tailxxxx.ts.net")
        client = _ssh_client(
            status_path=status_path, control_dir=control_dir, vault_path=vault_path,
            fake_vault=fake_vault, fresh_limiter=fresh_limiter,
            ssh_allowlist_path=ssh_allowlist_path, owner_token=owner_token,
        )

        r = client.request(
            "DELETE", "/api/v1/tailnet/ssh-hosts/db1.tailxxxx.ts.net",
        )

        assert r.status_code == 200
        remaining = {h["host"] for h in r.json()["hosts"]}
        assert remaining == {"build-box.tailxxxx.ts.net"}
        assert store.is_allowed("db1.tailxxxx.ts.net") is False
        assert store.is_allowed("build-box.tailxxxx.ts.net") is True

    def test_revoking_an_unknown_host_is_a_no_op_200(
        self, status_path, control_dir, vault_path, fake_vault, fresh_limiter,
        ssh_allowlist_path, owner_token,
    ) -> None:
        client = _ssh_client(
            status_path=status_path, control_dir=control_dir, vault_path=vault_path,
            fake_vault=fake_vault, fresh_limiter=fresh_limiter,
            ssh_allowlist_path=ssh_allowlist_path, owner_token=owner_token,
        )

        r = client.request(
            "DELETE", "/api/v1/tailnet/ssh-hosts/never-approved.tailxxxx.ts.net",
        )

        assert r.status_code == 200
        assert r.json() == {"hosts": []}
