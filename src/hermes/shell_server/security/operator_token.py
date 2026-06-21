"""OperatorToken — short-lived signed token for proxied runtime mutators.

Confused-deputy remediation (ALTO hallazgo, security-hardening branch).

Problem
-------
When the shell-server (process uid: hermes) proxies a mutator call to the
runtime D-Bus, the runtime sees `sender_uid = hermes_process_uid`.  That uid
is NOT the human operator (hermes-user).  Two failure modes:

  (A) hermes uid IS in authorized_uids  → confused-deputy: any HTTP client
      can mutate the runtime under the shell-server's identity.
  (B) hermes uid NOT in authorized_uids → legitimate proxy calls denied,
      operator cannot use the shell UI.

Hybrid model
------------
Local operator calls (GTK shell → D-Bus direct, uid = hermes-user) continue
unchanged — they are already correct.

Proxied calls (shell-server HTTP → D-Bus, uid = hermes) MUST carry a signed
OperatorToken so the runtime can:
  1. Reject calls that lack a valid token (fail-closed).
  2. Extract `operator_id` from the token — not from the proxy uid.
  3. Attribute audit entries to the verified operator, not the proxy.

Token format
------------
HMAC-SHA256 over a canonical payload:
    <operator_id>|<operation>|<issued_at_unix>|<expiry_unix>|<nonce>

Signed with a 32-byte subkey derived via HKDF from master.key:
    SecretsVault.derive_subkey(label="operator-token")

Security properties
-------------------
- HMAC verified with hmac.compare_digest (constant time, CWE-208).
- Short expiry (default 30 s) limits replay window.
- Nonce prevents exact-replay within the expiry window (optional, best-effort).
- Fail-closed: missing or invalid token → OperatorTokenError, no execution.
- Token never logged (contains operator_id, which is PII-adjacent).
- The subkey is stable per-install (derives from master.key), so tokens
  minted by one process are verifiable by another on the same machine.

Acuñación (minting)
-------------------
The token is minted where the operator is authenticated: the shell-server
session (the HTTP request has already come from localhost, the shell is
running as the operator).  The shell-server calls `OperatorTokenMinter.mint()`
and forwards the resulting token string in the D-Bus call body (separate
argument, not embedded in the text payload).

Verificación
-----------
`_authorize()` in DbusRuntimeServiceWiring:
  - If sender_uid ∈ authorized_uids → direct operator, no token needed.
  - Else if sender_uid == proxy_uid AND token present → verify token,
    extract operator_id.
  - Else → DbusAuthorizationError (fail-closed).
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from dataclasses import dataclass
from hashlib import sha256

logger = logging.getLogger("hermes.shell_server.security.operator_token")

# Token validity window.  30 s is generous for localhost IPC latency.
_DEFAULT_EXPIRY_S: int = 30

# Canonical field separator — chosen to be URL-safe and unlikely in field values.
_SEP = "|"


class OperatorTokenError(ValueError):
    """Raised when an operator token fails validation.

    Subclasses carry more specific context for logging; callers catch the
    base class and convert to DbusAuthorizationError (no details to client).
    """


class OperatorTokenExpired(OperatorTokenError):
    """Token is outside its validity window."""


class OperatorTokenForged(OperatorTokenError):
    """Token HMAC does not verify — tampered or wrong key."""


class OperatorTokenMalformed(OperatorTokenError):
    """Token string cannot be parsed into the expected fields."""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperatorTokenClaims:
    """Verified claims extracted from a valid token.

    Fields are provided by the minter and verified by the verifier.
    Never constructed by untrusted code — only returned by verify().
    """

    operator_id: str
    operation: str
    issued_at: int
    expiry: int
    nonce: str


# ---------------------------------------------------------------------------
# Minter
# ---------------------------------------------------------------------------


class OperatorTokenMinter:
    """Mints short-lived operator tokens for proxied runtime mutators.

    The signing_key MUST be derived via SecretsVault.derive_subkey(
        label="operator-token"
    ) so it is stable per-install and consistent across processes that share
    the same master.key.

    Thread-safe: mint() is a pure function (reads only immutable state).
    """

    def __init__(self, *, signing_key: bytes, expiry_s: int = _DEFAULT_EXPIRY_S) -> None:
        if len(signing_key) < 32:
            raise ValueError("signing_key must be at least 32 bytes")
        self._key = signing_key
        self._expiry_s = expiry_s

    def mint(self, *, operator_id: str, operation: str) -> str:
        """Return a signed token string for the given (operator, operation) pair.

        Args:
            operator_id: UUID string of the human operator (from session/keyring).
            operation:   The exact D-Bus method name this token authorizes.

        Returns:
            A compact token string: base64url(payload)|hmac_hex
        """
        issued_at = int(time.time())
        expiry = issued_at + self._expiry_s
        nonce = os.urandom(8).hex()
        payload = _build_payload(operator_id, operation, issued_at, expiry, nonce)
        sig = _sign(self._key, payload)
        return f"{payload}{_SEP}{sig}"


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class OperatorTokenVerifier:
    """Verifies tokens produced by OperatorTokenMinter.

    Clock tolerance: if the verifying process's clock is slightly behind the
    minting process, tokens near expiry could be incorrectly rejected.  We
    accept a 2-second tolerance in issued_at validation (not in expiry — that
    stays strict to limit the replay window).

    Thread-safe: verify() is a pure function.
    """

    _CLOCK_TOLERANCE_S = 2

    def __init__(self, *, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("signing_key must be at least 32 bytes")
        self._key = signing_key

    def verify(self, token: str, *, expected_operation: str | None = None) -> OperatorTokenClaims:
        """Verify the token and return its claims.

        Args:
            token:              The token string returned by OperatorTokenMinter.mint().
            expected_operation: If set, the token's operation field must match
                                exactly (prevents cross-operation token reuse).

        Returns:
            OperatorTokenClaims with the verified fields.

        Raises:
            OperatorTokenMalformed: token cannot be parsed.
            OperatorTokenForged:    HMAC does not verify.
            OperatorTokenExpired:   token outside validity window.
            OperatorTokenError:     operation mismatch.
        """
        claims, sig = _split_token(token)
        _verify_hmac(self._key, claims, sig)
        parsed = _parse_claims(claims)
        _check_expiry(parsed)
        if expected_operation is not None:
            _check_operation(parsed, expected_operation)
        return parsed


# ---------------------------------------------------------------------------
# Private helpers — kept ≤20 lines each
# ---------------------------------------------------------------------------


def _build_payload(
    operator_id: str, operation: str, issued_at: int, expiry: int, nonce: str
) -> str:
    """Canonical payload string for signing."""
    return _SEP.join([operator_id, operation, str(issued_at), str(expiry), nonce])


def _sign(key: bytes, payload: str) -> str:
    """HMAC-SHA256 over the payload, returned as hex."""
    return hmac.new(key, payload.encode("utf-8"), sha256).hexdigest()


def _split_token(token: str) -> tuple[str, str]:
    """Split token into (claims_payload, hmac_hex).

    Token format: <5-field payload>|<hmac_hex>
    Total separator count = 5 (4 in payload + 1 before hmac).
    """
    parts = token.split(_SEP)
    if len(parts) != 6:  # noqa: PLR2004
        raise OperatorTokenMalformed(
            f"Token has {len(parts)} fields, expected 6"
        )
    claims = _SEP.join(parts[:5])
    sig = parts[5]
    return claims, sig


def _verify_hmac(key: bytes, payload: str, provided_sig: str) -> None:
    """Constant-time HMAC comparison. Raises OperatorTokenForged on mismatch."""
    expected = _sign(key, payload)
    if not hmac.compare_digest(expected, provided_sig):
        raise OperatorTokenForged("Operator token HMAC verification failed")


def _parse_claims(payload: str) -> OperatorTokenClaims:
    """Parse the 5-field payload into OperatorTokenClaims."""
    parts = payload.split(_SEP)
    if len(parts) != 5:  # noqa: PLR2004
        raise OperatorTokenMalformed("Payload has wrong field count after split")
    try:
        issued_at = int(parts[2])
        expiry = int(parts[3])
    except ValueError as exc:
        raise OperatorTokenMalformed(f"Non-integer timestamp: {exc}") from exc
    return OperatorTokenClaims(
        operator_id=parts[0],
        operation=parts[1],
        issued_at=issued_at,
        expiry=expiry,
        nonce=parts[4],
    )


def _check_expiry(claims: OperatorTokenClaims) -> None:
    """Fail if the token is expired or not yet valid."""
    now = int(time.time())
    if now > claims.expiry:
        raise OperatorTokenExpired(
            f"Token expired at {claims.expiry}, now is {now}"
        )
    # Reject tokens issued too far in the future (clock skew / replay with
    # pre-minted tokens).
    tolerance = OperatorTokenVerifier._CLOCK_TOLERANCE_S
    if claims.issued_at > now + tolerance:
        raise OperatorTokenExpired(
            f"Token issued at {claims.issued_at} is in the future (now={now})"
        )


def _check_operation(claims: OperatorTokenClaims, expected: str) -> None:
    """Reject cross-operation token reuse."""
    if claims.operation != expected:
        raise OperatorTokenError(
            f"Token operation mismatch: expected {expected!r}, got {claims.operation!r}"
        )
