"""CloudflareAccessVerifier — OS-side JWT verification for the named tunnel.

WHY this exists
---------------
The Cloudflare Access edge enforces its policy (SSO login, allowed-user list)
and injects a signed `Cf-Access-Jwt-Assertion` header into every request that
passes its checks.  Without OS-side verification, that header is advisory:

  - A user who directly knows the tunnel URL and can craft a raw HTTP request
    to cloudflared (bypassing the Access edge somehow) arrives at the origin
    with no header — and the origin currently accepts it.
  - A mis-configured Access policy (e.g., a wildcard bypass rule) silently
    stops enforcing without the origin noticing.

This module verifies the header on the origin side:

  1. Fetches the team's JWKS (public keys) from the well-known Cloudflare
     endpoint once, then caches them for `_JWKS_TTL_SECONDS` so that key
     rotation is picked up without restarts.
  2. Verifies the RS256 signature using PyJWT against the fetched public key.
  3. Checks `aud` (the Access application AUD tag) and `exp`.
  4. Returns `CloudflareAccessClaims` on success; raises specific errors on
     every failure mode.  Callers treat any error as "deny".

Fail-closed design
------------------
- If `CF_ACCESS_AUD` / `CF_ACCESS_TEAM_DOMAIN` env vars are absent AND no
  config file entry is found, the verifier raises `CfAccessNotConfigured`.
  The caller MUST decide whether to deny or pass.  The MirrorServer default
  is: **deny** (no config → no remote stream).
- If JWKS fetching fails (network error, timeout) the cached key set is used
  if still valid; if the cache is empty the request is denied.

Configuration
-------------
Set by the OS operator (post-install, via /etc/hermes/cf-access.env):

    CF_ACCESS_TEAM_DOMAIN=your-team.cloudflareaccess.com
    CF_ACCESS_AUD=<AUD tag from Access application settings>

Or via environment variables injected by a systemd `EnvironmentFile=`.

DO NOT hard-code these values — they vary per deployment.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt

logger = logging.getLogger("hermes.mirror.cf_access")

# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

_CF_ACCESS_CONFIG_FILE = Path(
    os.environ.get("CF_ACCESS_CONFIG_FILE", "/etc/hermes/cf-access.env")
)

_JWKS_TTL_SECONDS: float = 300.0   # 5 minutes
_JWKS_FETCH_TIMEOUT: float = 8.0   # seconds
_ALLOWED_ALGORITHMS = ["RS256"]


@dataclass(frozen=True, slots=True)
class CloudflareAccessClaims:
    """Verified claims from a Cf-Access-Jwt-Assertion JWT.

    All fields are present only after full signature + aud + exp verification.
    Never construct this from untrusted data.
    """

    email: str          # The authenticated user's email address
    sub: str            # Cloudflare Access user identity (stable UUID)
    aud: str            # Access application AUD tag (verified against config)
    exp: int            # Unix expiry timestamp (already checked)
    iat: int            # Issued-at Unix timestamp


class CfAccessError(ValueError):
    """Base for all Cloudflare Access verification failures."""


class CfAccessNotConfigured(CfAccessError):
    """CF_ACCESS_AUD / CF_ACCESS_TEAM_DOMAIN not set — operator must configure."""


class CfAccessTokenMissing(CfAccessError):
    """The Cf-Access-Jwt-Assertion header is absent from the request."""


class CfAccessTokenExpired(CfAccessError):
    """The JWT has passed its exp claim."""


class CfAccessTokenInvalid(CfAccessError):
    """Signature invalid, wrong aud, or malformed JWT."""


class CfAccessJwksFetchError(CfAccessError):
    """Could not fetch the team JWKS and cache is empty."""


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_config_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file (no quotes, no export prefix).

    Lines starting with # and blank lines are skipped.
    Returns {} if the file does not exist.
    """
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _resolve_config() -> tuple[str, str]:
    """Return (team_domain, aud).  Raises CfAccessNotConfigured if absent."""
    file_conf = _load_config_file(_CF_ACCESS_CONFIG_FILE)
    team_domain = (
        os.environ.get("CF_ACCESS_TEAM_DOMAIN")
        or file_conf.get("CF_ACCESS_TEAM_DOMAIN", "")
    ).strip().rstrip("/")
    aud = (
        os.environ.get("CF_ACCESS_AUD")
        or file_conf.get("CF_ACCESS_AUD", "")
    ).strip()
    if not team_domain or not aud:
        raise CfAccessNotConfigured(
            "CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD must be set in env or "
            "/etc/hermes/cf-access.env before remote access is available."
        )
    return team_domain, aud


def _jwks_url(team_domain: str) -> str:
    return f"https://{team_domain}/cdn-cgi/access/certs"


# ---------------------------------------------------------------------------
# JWKS cache (thread-safe, TTL-based)
# ---------------------------------------------------------------------------


class _JwksCache:
    """Thread-safe TTL cache for the Cloudflare Access JWKS.

    Fetches the JWKS synchronously (blocking) on cache miss or expiry.
    The mirror server is async, but JWKS fetching happens rarely (every 5 min)
    and is short-lived (8 s timeout), so a blocking call on the threadpool is
    acceptable. The alternative (aiohttp) would require passing the event loop
    through the verifier, coupling it to the runtime.
    """

    def __init__(self, ttl_seconds: float = _JWKS_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._client: jwt.PyJWKClient | None = None
        self._last_fetch: float = 0.0
        self._team_domain: str = ""

    def get_signing_key(self, token: str, team_domain: str) -> jwt.PyJWK:
        """Return the signing key for *token*, refreshing the cache if needed."""
        with self._lock:
            if self._client is None or self._team_domain != team_domain:
                self._client = jwt.PyJWKClient(
                    _jwks_url(team_domain), cache_jwk_set=True
                )
                self._team_domain = team_domain
                self._last_fetch = 0.0  # force refresh

            now = time.monotonic()
            if now - self._last_fetch >= self._ttl:
                # PyJWKClient fetches on get_signing_key_from_jwt; we just
                # reset the last_fetch time after a successful fetch.
                # The actual network call is inside get_signing_key_from_jwt.
                self._last_fetch = now  # prevent thundering herd

        # Outside the lock: network call does not need the lock.
        try:
            signing_key = self._client.get_signing_key_from_jwt(token)
        except Exception as exc:
            raise CfAccessJwksFetchError(
                f"Failed to fetch/find signing key from Cloudflare JWKS: {exc}"
            ) from exc
        return signing_key


_jwks_cache = _JwksCache()


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class CloudflareAccessVerifier:
    """Verify Cf-Access-Jwt-Assertion headers from Cloudflare Access.

    Usage::

        verifier = CloudflareAccessVerifier()
        # In the WebSocket handler:
        try:
            claims = verifier.verify(token_from_header)
        except CfAccessNotConfigured:
            # operator hasn't configured Cloudflare Access → deny
            raise
        except CfAccessError:
            # any verification failure → deny
            raise

    Thread-safe: verify() is stateless beyond the shared JWKS cache.
    """

    def __init__(self, *, cache: _JwksCache | None = None) -> None:
        self._cache = cache or _jwks_cache

    def verify(self, raw_token: str) -> CloudflareAccessClaims:
        """Verify *raw_token* and return its claims.

        Args:
            raw_token: The value of the Cf-Access-Jwt-Assertion header (bare JWT).

        Returns:
            CloudflareAccessClaims with verified fields.

        Raises:
            CfAccessNotConfigured: operator hasn't set team domain + AUD.
            CfAccessTokenMissing:  raw_token is empty.
            CfAccessTokenExpired:  JWT exp has passed.
            CfAccessTokenInvalid:  signature wrong, aud mismatch, or malformed.
            CfAccessJwksFetchError: JWKS fetch failed and cache is empty.
        """
        if not raw_token or not raw_token.strip():
            raise CfAccessTokenMissing("Cf-Access-Jwt-Assertion header is absent or empty")

        team_domain, aud = _resolve_config()
        signing_key = self._cache.get_signing_key(raw_token, team_domain)

        try:
            payload: dict[str, Any] = jwt.decode(
                raw_token,
                signing_key.key,
                algorithms=_ALLOWED_ALGORITHMS,
                audience=aud,
                options={
                    "require": ["exp", "aud", "sub", "iat"],
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_signature": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise CfAccessTokenExpired("Cloudflare Access JWT is expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise CfAccessTokenInvalid(f"Cloudflare Access JWT audience mismatch: {exc}") from exc
        except jwt.DecodeError as exc:
            raise CfAccessTokenInvalid(f"Cloudflare Access JWT decode error: {exc}") from exc
        except jwt.PyJWTError as exc:
            raise CfAccessTokenInvalid(f"Cloudflare Access JWT invalid: {exc}") from exc

        return _build_claims(payload)


def _build_claims(payload: dict[str, Any]) -> CloudflareAccessClaims:
    """Map raw JWT payload dict to CloudflareAccessClaims.

    Extracts fields defensively — Cloudflare Access always populates these
    for authenticated users, but we never trust the payload before verify().
    """
    aud_raw = payload.get("aud", "")
    # PyJWT may return aud as str or list[str] depending on the token.
    aud_str = aud_raw[0] if isinstance(aud_raw, list) else str(aud_raw)
    return CloudflareAccessClaims(
        email=str(payload.get("email", "")),
        sub=str(payload.get("sub", "")),
        aud=aud_str,
        exp=int(payload["exp"]),
        iat=int(payload["iat"]),
    )
