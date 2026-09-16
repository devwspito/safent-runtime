"""CompanionSsoAuthority — daemon-side authority for the Safent -> safent-ads
session bridge (026, contracts/sso.md §3).

The daemon is the ONLY reader of the SSO private key and the ONLY signer of
owner assertions. The shell-server (transport, T005) never sees the key — it
asks the daemon over D-Bus for a fresh, short-lived, single-use assertion and
forwards ONLY the resulting opaque string to the companion.

The key is read from `/run/hermes/companions/ads-sso.key` — a root-staged
0440 root:hermes copy on tmpfs, NOT the raw `/etc/hermes/companions/
ads-sso.key` bind mount (provisioned 0400 by provision.sh). The daemon runs
as `User=hermes` (uid 880); the mount's uid inside the container is an ENGINE
artefact (root:root either way — see hermes.shell_server.companions' own
note on rootless-vs-rootful remap), never `hermes`, so a direct read of the
mount raises PermissionError. `hermes-companion-bearer`'s root
`ExecStartPre=-+` stages this copy every boot — EXACT same mechanism already
proven for the companion bearer (024); see
hermes.shell_server.companions.COMPANION_RUNTIME_SSO_KEY_PATH /
COMPANION_SSO_KEY_MOUNT_PATH.

Security invariants (verified by tests/unit/agents_os/
test_companion_sso_assertion.py):
  - The private key is loaded from a fixed path with the SAME ownership/
    permission check `hermes.shell_server.companions` applies to every other
    companion secret (`is_companion_secret_file_trustworthy`) — a file with
    loose permissions (writable by group/other, or not root-owned/not on a
    read-only mount) is refused, never read.
  - The key bytes NEVER appear in a returned value, an exception message, or
    a log line (`CompanionSsoKeyError` carries only the path and reason).
  - Every assertion gets a fresh UUIDv4 `jti` — never reused, so a captured
    assertion cannot be replayed once the companion's `sso_assertions_seen`
    table has consumed it (defense in depth; the true replay guard lives in
    safent-ads).
  - `exp - iat` is always exactly `_ASSERTION_TTL_SECONDS` (60 s per
    contracts/sso.md §3) — never operator-controlled.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

logger = logging.getLogger("hermes.agents_os.companion_sso_authority")

# Literal, not imported from hermes.shell_server.companions — this module
# stays import-light at load time (every hermes.* dependency below is a lazy,
# in-function import for the same reason). MUST equal
# hermes.shell_server.companions.COMPANION_RUNTIME_SSO_KEY_PATH — pinned by
# test_companion_sso_assertion.py::TestDefaultKeyPathIsTheRootStagedCopy.
_SSO_PRIVATE_KEY_PATH = Path("/run/hermes/companions/ads-sso.key")

# Payload literals — contracts/sso.md §3. Product constants, never overridden
# at runtime (a configurable `iss`/`aud`/`purpose` would let a caller mint an
# assertion for a different audience/purpose than the one the companion's
# verifier expects).
_ASSERTION_VERSION = 1
_ISSUER = "safent-runtime"
_AUDIENCE = "safent-ads"
_PURPOSE = "cockpit_session"
_SURFACE = "safent_cockpit"
_ASSERTION_TTL_SECONDS = 60
_SUBJECT_DOMAIN_SEPARATOR = b"ads-sso-subject"

_RATE_LIMIT_MAX_PER_MINUTE = 30
_RATE_LIMIT_WINDOW_SECONDS = 60.0


class CompanionSsoAuthorityError(RuntimeError):
    """Base class for every failure this module raises. Messages MUST NOT
    include key material — only the path and a human-readable reason."""


class CompanionSsoKeyUnavailableError(CompanionSsoAuthorityError):
    """The private key file is absent, unreadable, or fails the ownership/
    permission invariant (`is_companion_secret_file_trustworthy`)."""


class CompanionSsoRateLimitedError(CompanionSsoAuthorityError):
    """More than `_RATE_LIMIT_MAX_PER_MINUTE` assertions requested within
    `_RATE_LIMIT_WINDOW_SECONDS` (contracts/sso.md §3: `RATE_LIMITED`)."""


@dataclass(frozen=True)
class OwnerAssertion:
    """The wire shape `mint_companion_owner_assertion` returns over D-Bus —
    contracts/sso.md §3. `assertion` is opaque to every caller except the
    companion's verifier; nothing here is, or contains, the private key."""

    assertion: str
    expires_at: str


class _SlidingWindowRateLimiter:
    """In-process, single-process rate limit (30/min, contracts/sso.md §3).

    The daemon runs as a single process per host — a plain lock-guarded list
    is sufficient; no cross-process coordination is needed."""

    def __init__(
        self,
        *,
        max_per_window: int = _RATE_LIMIT_MAX_PER_MINUTE,
        window_seconds: float = _RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self._max = max_per_window
        self._window = window_seconds
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            if len(self._timestamps) >= self._max:
                return False
            self._timestamps.append(now)
            return True


def _load_sso_private_key(*, path: Path = _SSO_PRIVATE_KEY_PATH) -> Ed25519PrivateKey:
    """Load and decode the SSO Ed25519 private key.

    Fail-closed: any anomaly (missing file, loose permissions, malformed
    content) raises `CompanionSsoKeyUnavailableError` with NO key material in
    the message. Uses the SAME ownership/permission invariant every other
    companion secret is held to (`is_companion_secret_file_trustworthy`) —
    never re-implemented here.
    """
    from hermes.shell_server.companions import (  # noqa: PLC0415
        is_companion_secret_file_trustworthy,
    )

    if not is_companion_secret_file_trustworthy(path):
        raise CompanionSsoKeyUnavailableError(
            f"SSO private key at {path} is missing or fails the ownership/"
            "permission invariant — refusing to read it"
        )
    try:
        seed_b64 = path.read_text(encoding="utf-8").strip()
        seed = _b64_decode_std(seed_b64)
        return Ed25519PrivateKey.from_private_bytes(seed)
    except (OSError, ValueError, InvalidKey) as exc:
        raise CompanionSsoKeyUnavailableError(
            f"SSO private key at {path} could not be loaded ({type(exc).__name__})"
        ) from exc


def _b64_decode_std(value: str) -> bytes:
    import base64  # noqa: PLC0415

    return base64.b64decode(value, validate=True)


def _derive_subject() -> str:
    """`sub` = sha256(master.key ‖ "ads-sso-subject") — contracts/sso.md §3.
    Stable per install, not invertible, never the master key itself."""
    from hermes.shell_server.security.secrets import load_master_key  # noqa: PLC0415

    return hashlib.sha256(load_master_key() + _SUBJECT_DOMAIN_SEPARATOR).hexdigest()


def _b64url_encode(data: bytes) -> str:
    import base64  # noqa: PLC0415

    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _build_payload(*, slug: str, subject: str, now: int) -> bytes:
    """JSON compact, UTF-8, sorted keys — the signature covers these EXACT
    bytes (contracts/sso.md §3: never a re-serialized dict on the verifier
    side could drift from what was signed)."""
    payload = {
        "v": _ASSERTION_VERSION,
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "slug": slug,
        "sub": subject,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + _ASSERTION_TTL_SECONDS,
        "purpose": _PURPOSE,
        "surface": _SURFACE,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


class CompanionSsoAuthority:
    """Mints owner assertions and reports companion health. Stateless except
    for the in-process rate limiter — safe to construct once and reuse."""

    def __init__(self, *, private_key_path: Path = _SSO_PRIVATE_KEY_PATH) -> None:
        self._private_key_path = private_key_path
        self._rate_limiter = _SlidingWindowRateLimiter()

    def mint_owner_assertion(self, *, slug: str) -> OwnerAssertion:
        """Sign a fresh owner assertion for *slug* (contracts/sso.md §3).

        Fail-closed: an unreadable/untrustworthy key or an exhausted rate
        budget both raise — never a degraded/placeholder assertion.
        """
        if not self._rate_limiter.allow():
            logger.warning(
                "hermes.dbus.companion_sso_rate_limited", extra={"slug": slug}
            )
            raise CompanionSsoRateLimitedError(
                f"more than {_RATE_LIMIT_MAX_PER_MINUTE} assertions/minute requested"
            )

        private_key = _load_sso_private_key(path=self._private_key_path)
        subject = _derive_subject()
        now = int(time.time())
        payload_bytes = _build_payload(slug=slug, subject=subject, now=now)
        signature = private_key.sign(payload_bytes)

        assertion = f"{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"
        expires_at = _iso8601(now + _ASSERTION_TTL_SECONDS)
        logger.info(
            "hermes.dbus.companion_sso_assertion_minted",
            extra={"slug": slug, "expires_at": expires_at},
        )
        return OwnerAssertion(assertion=assertion, expires_at=expires_at)


def _iso8601(epoch_seconds: int) -> str:
    from datetime import UTC, datetime  # noqa: PLC0415

    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat().replace("+00:00", "Z")
