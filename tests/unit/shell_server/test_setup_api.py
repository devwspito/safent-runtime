"""Tests for POST /api/v1/setup/account.

Coverage:
  - Happy path: valid payload stages the file with 0600 permissions.
  - Response never contains the password.
  - Invalid username formats rejected with 400.
  - Password too short / too long rejected with 422 (Pydantic field validation).
  - Password with control characters (including \\n) rejected with 400.
  - Staged file contains expected keys (username, requested_at) but NOT password
    is NOT tested here because the file is internal — we test the HTTP contract.
  - File has 0600 permissions.
  - _validate_username covers edge cases.
  - _validate_password rejects control chars.
  - Second POST after sentinel exists returns 409 already_configured.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.setup.api import (
    _validate_password,
    _validate_username,
    create_setup_router,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stage_dir(tmp_path: Path) -> Path:
    d = tmp_path / "hermes-run" / "setup"
    d.mkdir(mode=0o700, parents=True)
    return d


@pytest.fixture
def sentinel_file(tmp_path: Path) -> Path:
    return tmp_path / "account-applied"


@pytest.fixture
def client(stage_dir: Path, sentinel_file: Path) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_setup_router(stage_dir=stage_dir, sentinel_file=sentinel_file)
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Validation unit tests (pure — no HTTP)
# ---------------------------------------------------------------------------


class TestUsernameValidation:
    def test_simple_lowercase_accepted(self) -> None:
        assert _validate_username("alice")

    def test_starts_with_digit_rejected(self) -> None:
        assert not _validate_username("1alice")

    def test_starts_with_uppercase_rejected(self) -> None:
        assert not _validate_username("Alice")

    def test_starts_with_hyphen_rejected(self) -> None:
        assert not _validate_username("-alice")

    def test_with_hyphen_and_underscore_accepted(self) -> None:
        assert _validate_username("hermes-user_01")

    def test_32_chars_accepted(self) -> None:
        # 1 leading char + 31 = 32 total
        assert _validate_username("a" + "b" * 31)

    def test_33_chars_rejected(self) -> None:
        assert not _validate_username("a" + "b" * 32)

    def test_empty_rejected(self) -> None:
        assert not _validate_username("")

    def test_space_inside_rejected(self) -> None:
        assert not _validate_username("alice bob")

    def test_uppercase_inside_rejected(self) -> None:
        assert not _validate_username("aliceBob")

    def test_special_chars_rejected(self) -> None:
        for char in ("!", "@", "/", "\\", ";", "`", "$"):
            assert not _validate_username(f"a{char}b"), f"should reject: a{char}b"


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_returns_staged_true(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/setup/account",
            json={"username": "hermes-user", "password": "supersecret123"},
        )
        assert r.status_code == 200
        assert r.json() == {"staged": True}

    def test_staged_file_created(self, client: TestClient, stage_dir: Path) -> None:
        client.post(
            "/api/v1/setup/account",
            json={"username": "testuser", "password": "password1234"},
        )
        staged = stage_dir / "account-request.json"
        assert staged.exists()

    def test_staged_file_has_0600_permissions(
        self, client: TestClient, stage_dir: Path
    ) -> None:
        client.post(
            "/api/v1/setup/account",
            json={"username": "testuser", "password": "password1234"},
        )
        staged = stage_dir / "account-request.json"
        mode = staged.stat().st_mode
        # Only owner read+write — no group, no other.
        assert stat.S_IMODE(mode) == 0o600

    def test_staged_file_contains_username_and_timestamp(
        self, client: TestClient, stage_dir: Path
    ) -> None:
        client.post(
            "/api/v1/setup/account",
            json={"username": "myuser", "password": "password1234"},
        )
        staged = stage_dir / "account-request.json"
        data = json.loads(staged.read_text())
        assert data["username"] == "myuser"
        assert "requested_at" in data

    def test_response_does_not_contain_password(self, client: TestClient) -> None:
        password = "supersecret9999"
        r = client.post(
            "/api/v1/setup/account",
            json={"username": "myuser", "password": password},
        )
        assert password not in r.text

    def test_second_call_overwrites_staged_file(
        self, client: TestClient, stage_dir: Path
    ) -> None:
        client.post(
            "/api/v1/setup/account",
            json={"username": "usera", "password": "password1234"},
        )
        client.post(
            "/api/v1/setup/account",
            json={"username": "userb", "password": "password5678"},
        )
        staged = stage_dir / "account-request.json"
        data = json.loads(staged.read_text())
        assert data["username"] == "userb"


# ---------------------------------------------------------------------------
# _validate_password unit tests (pure — no HTTP)
# ---------------------------------------------------------------------------


class TestPasswordValidation:
    def test_clean_password_accepted(self) -> None:
        assert _validate_password("Aaaaaaa1")

    def test_newline_injection_rejected(self) -> None:
        # REGRESSION for CRITICAL: chpasswd reads stdin line-by-line.
        # A \\n in the password injects a second 'user:pass' entry.
        # e.g. "Aaaaaaa1\\nroot:x" would set root's password on a vulnerable version.
        assert not _validate_password("Aaaaaaa1\nroot:x")

    def test_carriage_return_rejected(self) -> None:
        assert not _validate_password("Aaaaaaa1\r")

    def test_null_byte_rejected(self) -> None:
        assert not _validate_password("Aaaaaaa1\x00")

    def test_tab_rejected(self) -> None:
        assert not _validate_password("Aaaaaaa1\t")

    def test_del_char_rejected(self) -> None:
        assert not _validate_password("Aaaaaaa1\x7f")

    def test_other_c0_control_rejected(self) -> None:
        # ESC, BEL, BS — any C0 control char
        for code in (0x01, 0x07, 0x08, 0x1B, 0x1F):
            assert not _validate_password("Aaaaaaa1" + chr(code)), (
                f"should reject C0 char 0x{code:02x}"
            )

    def test_printable_ascii_accepted(self) -> None:
        # space (0x20) is the first non-control char — must be accepted
        assert _validate_password("Aaaa aa1")

    def test_high_unicode_accepted(self) -> None:
        # Non-ASCII printable chars are fine
        assert _validate_password("Aaaaaaa1é")

    def test_short_rejected(self) -> None:
        assert not _validate_password("Aaa1")

    def test_too_long_rejected(self) -> None:
        assert not _validate_password("a" * 257)


# ---------------------------------------------------------------------------
# Rejection tests
# ---------------------------------------------------------------------------


class TestUsernameRejection:
    def test_starts_with_digit_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/setup/account",
            json={"username": "1badstart", "password": "password1234"},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "invalid_username"

    def test_uppercase_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/setup/account",
            json={"username": "BadUser", "password": "password1234"},
        )
        assert r.status_code == 400

    def test_special_char_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/setup/account",
            json={"username": "a;injection", "password": "password1234"},
        )
        assert r.status_code == 400

    def test_too_long_username_rejected_by_pydantic(self, client: TestClient) -> None:
        # Pydantic max_length=32 fires before our regex check.
        r = client.post(
            "/api/v1/setup/account",
            json={"username": "a" * 33, "password": "password1234"},
        )
        assert r.status_code == 422


class TestPasswordRejection:
    def test_too_short_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/setup/account",
            json={"username": "validuser", "password": "short"},
        )
        assert r.status_code == 422

    def test_too_long_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/setup/account",
            json={"username": "validuser", "password": "x" * 257},
        )
        assert r.status_code == 422

    def test_exactly_8_chars_accepted(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/setup/account",
            json={"username": "validuser", "password": "12345678"},
        )
        assert r.status_code == 200

    def test_exactly_256_chars_accepted(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/setup/account",
            json={"username": "validuser", "password": "x" * 256},
        )
        assert r.status_code == 200

    def test_password_with_newline_returns_400(self, client: TestClient) -> None:
        # REGRESSION for CRITICAL: newline injection into chpasswd stdin.
        r = client.post(
            "/api/v1/setup/account",
            json={"username": "validuser", "password": "Aaaaaaa1\nroot:x"},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "invalid_password"

    def test_password_with_null_byte_returns_400(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/setup/account",
            json={"username": "validuser", "password": "Aaaaaaa1\x00"},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "invalid_password"


# ---------------------------------------------------------------------------
# Sentinel gate tests (one-time enforcement)
# ---------------------------------------------------------------------------


class TestSentinelGate:
    def test_post_after_sentinel_exists_returns_409(
        self,
        stage_dir: Path,
        sentinel_file: Path,
    ) -> None:
        sentinel_file.write_text("{}")
        app = FastAPI()
        app.include_router(
            create_setup_router(stage_dir=stage_dir, sentinel_file=sentinel_file)
        )
        locked_client = TestClient(app)
        r = locked_client.post(
            "/api/v1/setup/account",
            json={"username": "validuser", "password": "password1234"},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "already_configured"

    def test_409_does_not_stage_file(
        self,
        stage_dir: Path,
        sentinel_file: Path,
    ) -> None:
        sentinel_file.write_text("{}")
        app = FastAPI()
        app.include_router(
            create_setup_router(stage_dir=stage_dir, sentinel_file=sentinel_file)
        )
        locked_client = TestClient(app)
        locked_client.post(
            "/api/v1/setup/account",
            json={"username": "validuser", "password": "password1234"},
        )
        staged = stage_dir / "account-request.json"
        assert not staged.exists()
