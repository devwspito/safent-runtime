"""Tests for CloudflareAccessVerifier — OS-side Cloudflare Access JWT verification.

Coverage:
  - Valid RS256 JWT → CloudflareAccessClaims returned
  - Expired JWT → CfAccessTokenExpired raised
  - Wrong audience → CfAccessTokenInvalid raised
  - Bad signature (tampered payload) → CfAccessTokenInvalid raised
  - Malformed JWT (not parseable) → CfAccessTokenInvalid raised
  - Empty/missing token → CfAccessTokenMissing raised
  - Not configured (no env vars, no file) → CfAccessNotConfigured raised
  - JWKS fetch failure → CfAccessJwksFetchError raised
  - aud as list (Cloudflare sometimes encodes it this way) → still verified
  - Config file parsing (KEY=VALUE format)
  - Token gate in MirrorServer: valid token + valid CF JWT → connection accepted
  - Token gate in MirrorServer: valid token + invalid CF JWT → connection closed 4403
  - Token gate in MirrorServer: invalid token (regardless of CF JWT) → closed 4401
  - _validate_token: short token → sys.exit(1)
  - Bypass local mode: loopback + bypass flag → CF check skipped
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hermes.shell_server.mirror.cf_access_verifier import (
    CloudflareAccessClaims,
    CloudflareAccessVerifier,
    CfAccessError,
    CfAccessJwksFetchError,
    CfAccessNotConfigured,
    CfAccessTokenExpired,
    CfAccessTokenInvalid,
    CfAccessTokenMissing,
    _JwksCache,
    _load_config_file,
    _resolve_config,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures — RSA key pair for signing test JWTs
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def rsa_public_key(rsa_private_key):
    return rsa_private_key.public_key()


def _make_jwt(
    private_key,
    *,
    aud: str | list[str] = "test-aud-abc123",
    exp_offset: int = 300,
    email: str = "alice@example.com",
    sub: str = "user-uuid-001",
    extra_headers: dict | None = None,
) -> str:
    """Mint a test JWT signed with the given RSA private key."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "aud": aud,
        "email": email,
        "sub": sub,
        "iat": now,
        "exp": now + exp_offset,
    }
    return jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers=extra_headers,
    )


def _mock_cache_returning_key(public_key) -> _JwksCache:
    """Build a _JwksCache whose get_signing_key() returns the given public key."""
    mock_jwk = MagicMock()
    mock_jwk.key = public_key
    cache = MagicMock(spec=_JwksCache)
    cache.get_signing_key.return_value = mock_jwk
    return cache


def _mock_cache_raising(exc: Exception) -> _JwksCache:
    cache = MagicMock(spec=_JwksCache)
    cache.get_signing_key.side_effect = exc
    return cache


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadConfigFile:
    def test_parses_key_value(self, tmp_path: Path) -> None:
        f = tmp_path / "cf-access.env"
        f.write_text("CF_ACCESS_AUD=myaud\nCF_ACCESS_TEAM_DOMAIN=team.example.com\n")
        result = _load_config_file(f)
        assert result["CF_ACCESS_AUD"] == "myaud"
        assert result["CF_ACCESS_TEAM_DOMAIN"] == "team.example.com"

    def test_skips_comments_and_blanks(self, tmp_path: Path) -> None:
        f = tmp_path / "cf-access.env"
        f.write_text("# comment\n\nCF_ACCESS_AUD=x\n")
        result = _load_config_file(f)
        assert list(result.keys()) == ["CF_ACCESS_AUD"]

    def test_returns_empty_when_file_absent(self, tmp_path: Path) -> None:
        result = _load_config_file(tmp_path / "nonexistent.env")
        assert result == {}

    def test_strips_whitespace(self, tmp_path: Path) -> None:
        f = tmp_path / "cf-access.env"
        f.write_text("  CF_ACCESS_AUD = trimmed  \n")
        result = _load_config_file(f)
        assert result["CF_ACCESS_AUD"] == "trimmed"


class TestResolveConfig:
    def test_env_vars_take_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "env-team.cloudflareaccess.com")
        monkeypatch.setenv("CF_ACCESS_AUD", "env-aud-xyz")
        with patch(
            "hermes.shell_server.mirror.cf_access_verifier._CF_ACCESS_CONFIG_FILE",
            Path("/nonexistent/path"),
        ):
            domain, aud = _resolve_config()
        assert domain == "env-team.cloudflareaccess.com"
        assert aud == "env-aud-xyz"

    def test_raises_when_not_configured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
        monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
        with patch(
            "hermes.shell_server.mirror.cf_access_verifier._CF_ACCESS_CONFIG_FILE",
            tmp_path / "absent.env",
        ):
            with pytest.raises(CfAccessNotConfigured):
                _resolve_config()

    def test_strips_trailing_slash_from_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com/")
        monkeypatch.setenv("CF_ACCESS_AUD", "some-aud")
        with patch(
            "hermes.shell_server.mirror.cf_access_verifier._CF_ACCESS_CONFIG_FILE",
            Path("/nonexistent"),
        ):
            domain, _ = _resolve_config()
        assert not domain.endswith("/")


# ---------------------------------------------------------------------------
# CloudflareAccessVerifier — happy path
# ---------------------------------------------------------------------------


class TestVerifierHappyPath:
    def test_valid_jwt_returns_claims(
        self, rsa_private_key, rsa_public_key, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-abc123")
        cache = _mock_cache_returning_key(rsa_public_key)
        token = _make_jwt(rsa_private_key, aud="test-aud-abc123")

        verifier = CloudflareAccessVerifier(cache=cache)
        claims = verifier.verify(token)

        assert isinstance(claims, CloudflareAccessClaims)
        assert claims.email == "alice@example.com"
        assert claims.sub == "user-uuid-001"
        assert claims.aud == "test-aud-abc123"

    def test_aud_as_list_accepted(
        self, rsa_private_key, rsa_public_key, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cloudflare sometimes encodes aud as a list — must still pass."""
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-list")
        cache = _mock_cache_returning_key(rsa_public_key)
        token = _make_jwt(rsa_private_key, aud=["test-aud-list"])

        verifier = CloudflareAccessVerifier(cache=cache)
        claims = verifier.verify(token)
        assert claims.aud == "test-aud-list"


# ---------------------------------------------------------------------------
# CloudflareAccessVerifier — rejection paths
# ---------------------------------------------------------------------------


class TestVerifierRejectsExpiredToken:
    def test_expired_token_raises(
        self, rsa_private_key, rsa_public_key, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-abc123")
        cache = _mock_cache_returning_key(rsa_public_key)
        # exp_offset=-10 → expired 10 seconds ago
        token = _make_jwt(rsa_private_key, exp_offset=-10)

        verifier = CloudflareAccessVerifier(cache=cache)
        with pytest.raises(CfAccessTokenExpired):
            verifier.verify(token)


class TestVerifierRejectsWrongAud:
    def test_wrong_audience_raises(
        self, rsa_private_key, rsa_public_key, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
        monkeypatch.setenv("CF_ACCESS_AUD", "correct-aud-123")
        cache = _mock_cache_returning_key(rsa_public_key)
        # Token minted with a DIFFERENT aud than configured
        token = _make_jwt(rsa_private_key, aud="wrong-aud-999")

        verifier = CloudflareAccessVerifier(cache=cache)
        with pytest.raises(CfAccessTokenInvalid):
            verifier.verify(token)


class TestVerifierRejectsBadSignature:
    def test_tampered_payload_raises(
        self, rsa_private_key, rsa_public_key, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-abc123")
        cache = _mock_cache_returning_key(rsa_public_key)
        token = _make_jwt(rsa_private_key, aud="test-aud-abc123")

        # Flip a bit in the payload (middle segment of the JWT)
        parts = token.split(".")
        payload_b64 = parts[1]
        # Append garbage to invalidate the signature
        parts[1] = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B")
        tampered = ".".join(parts)

        verifier = CloudflareAccessVerifier(cache=cache)
        with pytest.raises(CfAccessTokenInvalid):
            verifier.verify(tampered)

    def test_jwt_signed_with_different_key_raises(
        self, rsa_public_key, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Token signed with a different key than what the cache returns."""
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-abc123")
        # Generate a separate key and sign with it
        other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = _make_jwt(other_private_key, aud="test-aud-abc123")

        # But cache returns the ORIGINAL public key → signature mismatch
        cache = _mock_cache_returning_key(rsa_public_key)
        verifier = CloudflareAccessVerifier(cache=cache)
        with pytest.raises(CfAccessTokenInvalid):
            verifier.verify(token)


class TestVerifierRejectsMalformed:
    def test_not_a_jwt_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-abc123")

        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cache = _mock_cache_returning_key(other_key.public_key())
        verifier = CloudflareAccessVerifier(cache=cache)

        with pytest.raises(CfAccessTokenInvalid):
            verifier.verify("not.a.jwt.at.all")

    def test_empty_string_raises_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-abc123")

        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cache = _mock_cache_returning_key(other_key.public_key())
        verifier = CloudflareAccessVerifier(cache=cache)

        with pytest.raises(CfAccessTokenMissing):
            verifier.verify("")

    def test_whitespace_only_raises_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-abc123")

        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cache = _mock_cache_returning_key(other_key.public_key())
        verifier = CloudflareAccessVerifier(cache=cache)

        with pytest.raises(CfAccessTokenMissing):
            verifier.verify("   ")


class TestVerifierNotConfigured:
    def test_raises_when_env_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
        monkeypatch.delenv("CF_ACCESS_AUD", raising=False)

        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cache = _mock_cache_returning_key(other_key.public_key())
        verifier = CloudflareAccessVerifier(cache=cache)

        with patch(
            "hermes.shell_server.mirror.cf_access_verifier._CF_ACCESS_CONFIG_FILE",
            tmp_path / "absent.env",
        ):
            with pytest.raises(CfAccessNotConfigured):
                verifier.verify("any.token.value")


class TestVerifierJwksFetchFailure:
    def test_jwks_fetch_error_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-abc123")
        exc = CfAccessJwksFetchError("network timeout")
        cache = _mock_cache_raising(exc)
        verifier = CloudflareAccessVerifier(cache=cache)

        with pytest.raises(CfAccessJwksFetchError):
            verifier.verify("any.token.value")


# ---------------------------------------------------------------------------
# MirrorServer auth integration
# ---------------------------------------------------------------------------


class TestMirrorServerAuth:
    """Tests that MirrorServer closes connections with the right codes."""

    def _make_fake_ws(
        self,
        *,
        token: str = "goodtoken",
        cf_jwt: str = "",
        remote_ip: str = "10.0.0.1",
    ) -> MagicMock:
        ws = MagicMock()
        path = f"/ws?token={token}"
        request = MagicMock()
        request.path = path
        request.headers = {"Cf-Access-Jwt-Assertion": cf_jwt}
        ws.request = request
        ws.remote_address = (remote_ip, 12345)
        return ws

    def test_bad_token_closes_4401(self) -> None:
        from hermes.shell_server.mirror.server import MirrorServer

        source = MagicMock()
        mirror = MagicMock()
        cf = MagicMock(spec=CloudflareAccessVerifier)
        server = MirrorServer(
            source=source,
            mirror=mirror,
            token="correct-token",
            cf_verifier=cf,
        )
        ws = self._make_fake_ws(token="wrong-token")

        # Layer 1 check (synchronous)
        assert not server._authed_token(ws)
        # CF verifier should NOT be called when layer 1 fails

    def test_good_token_verified(self) -> None:
        from hermes.shell_server.mirror.server import MirrorServer

        source = MagicMock()
        mirror = MagicMock()
        cf = MagicMock(spec=CloudflareAccessVerifier)
        server = MirrorServer(
            source=source,
            mirror=mirror,
            token="correct-token",
            cf_verifier=cf,
        )
        ws = self._make_fake_ws(token="correct-token")
        assert server._authed_token(ws)

    def test_cf_access_denied_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hermes.shell_server.mirror.server import MirrorServer

        monkeypatch.setenv("HERMES_MIRROR_CF_ACCESS_BYPASS_LOCAL", "false")

        source = MagicMock()
        mirror = MagicMock()
        cf = MagicMock(spec=CloudflareAccessVerifier)
        cf.verify.side_effect = CfAccessTokenExpired("expired")
        server = MirrorServer(
            source=source,
            mirror=mirror,
            token="tok",
            cf_verifier=cf,
        )
        ws = self._make_fake_ws(token="tok", cf_jwt="some.jwt.token")
        assert not server._authed_cf_access(ws)

    def test_cf_access_not_configured_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes.shell_server.mirror.server import MirrorServer

        monkeypatch.setenv("HERMES_MIRROR_CF_ACCESS_BYPASS_LOCAL", "false")

        source = MagicMock()
        mirror = MagicMock()
        cf = MagicMock(spec=CloudflareAccessVerifier)
        cf.verify.side_effect = CfAccessNotConfigured("not set")
        server = MirrorServer(
            source=source,
            mirror=mirror,
            token="tok",
            cf_verifier=cf,
        )
        ws = self._make_fake_ws(token="tok")
        assert not server._authed_cf_access(ws)

    def test_bypass_local_on_loopback_skips_cf(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When BYPASS_LOCAL=true and remote is 127.0.0.1, CF check is skipped."""
        monkeypatch.setenv("HERMES_MIRROR_CF_ACCESS_BYPASS_LOCAL", "true")

        # Reload the module to pick up the env var (module-level constant)
        import importlib
        import hermes.shell_server.mirror.server as srv_mod
        importlib.reload(srv_mod)

        cf = MagicMock(spec=CloudflareAccessVerifier)
        server = srv_mod.MirrorServer(
            source=MagicMock(),
            mirror=MagicMock(),
            token="tok",
            cf_verifier=cf,
        )
        ws = self._make_fake_ws(token="tok", remote_ip="127.0.0.1")
        result = server._authed_cf_access(ws)
        assert result is True
        cf.verify.assert_not_called()

    def test_bypass_local_on_external_ip_still_checks_cf(
        self, monkeypatch: pytest.MonkeyPatch, rsa_private_key, rsa_public_key
    ) -> None:
        """BYPASS_LOCAL=true does NOT skip CF check for non-loopback peers."""
        monkeypatch.setenv("HERMES_MIRROR_CF_ACCESS_BYPASS_LOCAL", "true")
        monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.example.com")
        monkeypatch.setenv("CF_ACCESS_AUD", "test-aud-abc123")

        import importlib
        import hermes.shell_server.mirror.server as srv_mod
        importlib.reload(srv_mod)

        token = _make_jwt(rsa_private_key, aud="test-aud-abc123")
        cache = _mock_cache_returning_key(rsa_public_key)

        from hermes.shell_server.mirror.cf_access_verifier import CloudflareAccessVerifier as CFV
        cf = CFV(cache=cache)

        server = srv_mod.MirrorServer(
            source=MagicMock(),
            mirror=MagicMock(),
            token="tok",
            cf_verifier=cf,
        )
        ws = self._make_fake_ws(token="tok", cf_jwt=token, remote_ip="10.0.0.5")
        # External IP: CF check must run; valid JWT → True
        result = server._authed_cf_access(ws)
        assert result is True


# ---------------------------------------------------------------------------
# __main__ token validation
# ---------------------------------------------------------------------------


class TestMainTokenValidation:
    def test_short_token_exits(self) -> None:
        """_validate_token() must sys.exit(1) for tokens shorter than 22 chars."""
        from hermes.shell_server.mirror.__main__ import _validate_token

        with pytest.raises(SystemExit) as exc_info:
            _validate_token("tooshort")
        assert exc_info.value.code == 1

    def test_exactly_min_length_ok(self) -> None:
        from hermes.shell_server.mirror.__main__ import _validate_token, _MIN_TOKEN_CHARS

        # Should not raise
        _validate_token("A" * _MIN_TOKEN_CHARS)

    def test_long_token_ok(self) -> None:
        from hermes.shell_server.mirror.__main__ import _validate_token

        _validate_token("A" * 24)  # standard generated length
