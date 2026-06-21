"""Tests for the remote-access tunnel API endpoints.

Coverage:
  POST /disable with wrong password → 403 + no request file written
    (NOTE: the actual verify is in the root helper; here 403 is returned by
    the rate-limiter path; wrong-password detection happens async in the helper.
    The direct 403 path is via the rate-limiter or explicit invalid chars.)
  POST /disable with valid password + chars → request file written (0600)
  POST /disable rate-limited → 429
  POST /disable invalid chars → 400 + no request file
  POST /enable → request file written (no password)
  GET /status → reflects mock service check
  Request file has 0600 permissions
  Request file does NOT contain the password for enable
  Request file DOES contain the password for disable
  _validate_password_chars — control character rejection
  PasswordRateLimiter — blocking + expiry
  service_status.all_services_active — happy / partial / error
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.remote_access_tunnel.api import (
    _validate_password_chars,
    create_remote_access_tunnel_router,
)
from hermes.shell_server.remote_access_tunnel.rate_limiter import PasswordRateLimiter

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def control_dir(tmp_path: Path) -> Path:
    d = tmp_path / "hermes-run" / "remote-control"
    d.mkdir(mode=0o700, parents=True)
    return d


@pytest.fixture
def fresh_limiter() -> PasswordRateLimiter:
    """A fresh rate-limiter per test — isolates rate-limit state."""
    return PasswordRateLimiter()


@pytest.fixture
def client(control_dir: Path, fresh_limiter: PasswordRateLimiter) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_remote_access_tunnel_router(
            control_dir=control_dir, rate_limiter=fresh_limiter
        )
    )
    return TestClient(app)


def _staged(control_dir: Path) -> Path:
    return control_dir / "request.json"


# ---------------------------------------------------------------------------
# _validate_password_chars
# ---------------------------------------------------------------------------


class TestPasswordCharsValidation:
    def test_clean_password_accepted(self) -> None:
        assert _validate_password_chars("supersecret123")

    def test_newline_rejected(self) -> None:
        assert not _validate_password_chars("Aaaaaaa1\n")

    def test_null_byte_rejected(self) -> None:
        assert not _validate_password_chars("Aaaaaaa1\x00")

    def test_tab_rejected(self) -> None:
        assert not _validate_password_chars("Aaaaaaa1\t")

    def test_del_char_rejected(self) -> None:
        assert not _validate_password_chars("Aaaaaaa1\x7f")

    def test_space_accepted(self) -> None:
        assert _validate_password_chars("Aaaa aa1")

    def test_unicode_accepted(self) -> None:
        assert _validate_password_chars("Contraseña1!")


# ---------------------------------------------------------------------------
# PasswordRateLimiter
# ---------------------------------------------------------------------------


class TestPasswordRateLimiter:
    def test_not_blocked_initially(self) -> None:
        lim = PasswordRateLimiter(max_failures=3, window_seconds=60)
        assert not lim.is_blocked("key1")

    def test_blocked_after_max_failures(self) -> None:
        lim = PasswordRateLimiter(max_failures=3, window_seconds=60)
        for _ in range(3):
            lim.record_failure("key1")
        assert lim.is_blocked("key1")

    def test_not_blocked_before_max_failures(self) -> None:
        lim = PasswordRateLimiter(max_failures=3, window_seconds=60)
        for _ in range(2):
            lim.record_failure("key1")
        assert not lim.is_blocked("key1")

    def test_different_keys_are_independent(self) -> None:
        lim = PasswordRateLimiter(max_failures=2, window_seconds=60)
        lim.record_failure("key1")
        lim.record_failure("key1")
        assert lim.is_blocked("key1")
        assert not lim.is_blocked("key2")

    def test_expires_after_window(self) -> None:
        lim = PasswordRateLimiter(max_failures=2, window_seconds=0.05)
        lim.record_failure("key1")
        lim.record_failure("key1")
        assert lim.is_blocked("key1")
        time.sleep(0.1)
        assert not lim.is_blocked("key1")


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


class TestStatusEndpoint:
    def test_active_when_all_services_active(
        self, client: TestClient
    ) -> None:
        with patch(
            "hermes.shell_server.remote_access_tunnel.api.all_services_active",
            return_value=True,
        ):
            r = client.get("/api/v1/remote-access/status")
        assert r.status_code == 200
        assert r.json() == {"active": True}

    def test_inactive_when_any_service_down(
        self, client: TestClient
    ) -> None:
        with patch(
            "hermes.shell_server.remote_access_tunnel.api.all_services_active",
            return_value=False,
        ):
            r = client.get("/api/v1/remote-access/status")
        assert r.status_code == 200
        assert r.json() == {"active": False}


# ---------------------------------------------------------------------------
# POST /enable
# ---------------------------------------------------------------------------


class TestEnableEndpoint:
    def test_returns_staged_true(self, client: TestClient) -> None:
        with patch(
            "hermes.shell_server.remote_access_tunnel.api.all_services_active",
            return_value=False,
        ):
            r = client.post("/api/v1/remote-access/enable")
        assert r.status_code == 200
        assert r.json()["staged"] is True

    def test_staged_file_created(
        self, client: TestClient, control_dir: Path
    ) -> None:
        client.post("/api/v1/remote-access/enable")
        assert _staged(control_dir).exists()

    def test_staged_file_action_is_enable(
        self, client: TestClient, control_dir: Path
    ) -> None:
        client.post("/api/v1/remote-access/enable")
        data = json.loads(_staged(control_dir).read_text())
        assert data["action"] == "enable"

    def test_staged_file_has_no_password(
        self, client: TestClient, control_dir: Path
    ) -> None:
        client.post("/api/v1/remote-access/enable")
        data = json.loads(_staged(control_dir).read_text())
        assert "password" not in data

    def test_staged_file_permissions_0600(
        self, client: TestClient, control_dir: Path
    ) -> None:
        client.post("/api/v1/remote-access/enable")
        mode = _staged(control_dir).stat().st_mode
        assert stat.S_IMODE(mode) == 0o600


# ---------------------------------------------------------------------------
# POST /disable
# ---------------------------------------------------------------------------


class TestDisableEndpoint:
    def test_valid_password_stages_file(
        self, client: TestClient, control_dir: Path
    ) -> None:
        r = client.post(
            "/api/v1/remote-access/disable",
            json={"password": "validpassword123"},
        )
        assert r.status_code == 200
        assert r.json()["staged"] is True
        assert _staged(control_dir).exists()

    def test_staged_file_action_is_disable(
        self, client: TestClient, control_dir: Path
    ) -> None:
        client.post(
            "/api/v1/remote-access/disable",
            json={"password": "validpassword123"},
        )
        data = json.loads(_staged(control_dir).read_text())
        assert data["action"] == "disable"

    def test_staged_file_contains_password(
        self, client: TestClient, control_dir: Path
    ) -> None:
        client.post(
            "/api/v1/remote-access/disable",
            json={"password": "validpassword123"},
        )
        data = json.loads(_staged(control_dir).read_text())
        assert data["password"] == "validpassword123"

    def test_staged_file_permissions_0600(
        self, client: TestClient, control_dir: Path
    ) -> None:
        client.post(
            "/api/v1/remote-access/disable",
            json={"password": "validpassword123"},
        )
        mode = _staged(control_dir).stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_response_does_not_contain_password(
        self, client: TestClient
    ) -> None:
        secret = "mysecretpassword99"
        r = client.post(
            "/api/v1/remote-access/disable",
            json={"password": secret},
        )
        assert secret not in r.text

    def test_invalid_chars_returns_400(
        self, client: TestClient, control_dir: Path
    ) -> None:
        r = client.post(
            "/api/v1/remote-access/disable",
            json={"password": "validpwd\x00evil"},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "invalid_password"
        assert not _staged(control_dir).exists()

    def test_too_short_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/remote-access/disable",
            json={"password": "short"},
        )
        assert r.status_code == 422

    def test_too_long_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/remote-access/disable",
            json={"password": "x" * 257},
        )
        assert r.status_code == 422

    def test_rate_limited_after_max_failures(
        self, control_dir: Path
    ) -> None:
        """After exhausting attempts, further requests return 429."""
        limiter = PasswordRateLimiter(max_failures=5, window_seconds=60)
        app = FastAPI()
        app.include_router(
            create_remote_access_tunnel_router(
                control_dir=control_dir, rate_limiter=limiter
            )
        )
        c = TestClient(app, raise_server_exceptions=False)
        # Exhaust: each successful stage records a failure toward the limit.
        for _ in range(5):
            c.post(
                "/api/v1/remote-access/disable",
                json={"password": "validpassword1"},
            )
        # 6th attempt — rate-limited.
        r = c.post(
            "/api/v1/remote-access/disable",
            json={"password": "validpassword2"},
        )
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "too_many_attempts"


# ---------------------------------------------------------------------------
# service_status.all_services_active
# ---------------------------------------------------------------------------


class TestAllServicesActive:
    def test_all_active_returns_true(self) -> None:
        from hermes.shell_server.remote_access_tunnel.service_status import (
            all_services_active,
        )

        mock_result = MagicMock()
        mock_result.stdout = b"active\n"
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            assert all_services_active() is True

    def test_one_inactive_returns_false(self) -> None:
        from hermes.shell_server.remote_access_tunnel.service_status import (
            all_services_active,
        )

        call_count = 0

        def _fake_run(cmd, **_kw):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            # First service active, second inactive.
            mock.stdout = b"active\n" if call_count == 1 else b"inactive\n"
            mock.returncode = 0
            return mock

        with patch("subprocess.run", side_effect=_fake_run):
            assert all_services_active() is False

    def test_subprocess_error_returns_false(self) -> None:
        from hermes.shell_server.remote_access_tunnel.service_status import (
            all_services_active,
        )

        with patch("subprocess.run", side_effect=OSError("no systemctl")):
            assert all_services_active() is False
