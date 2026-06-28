"""Owner MFA for the elevation/approval gate (P4).

The product is single-owner and LOCAL — there is no username/password. Instead the
owner enrolls a TOTP authenticator (RFC 6238, Google-Authenticator compatible) ONCE,
and every elevation approval requires a fresh code. Delicate actions add a humanity
check; the most delicate add an enrolled human riddle. This defends against the agent
self-approving its own elevation: the agent (uid 999, sandboxed, no master.key) cannot
read the TOTP secret, so it cannot mint a valid code — "no me importa con qué historia
vengas, por aquí no pasas sin aprobar".

NO new dependency: TOTP is ~20 lines of stdlib (hmac/base64/struct/time). The secret
lives in an owner-only 0600 file the agent cannot reach (InaccessiblePaths + the agent
is in a separate sandbox without /var/lib/hermes mounted).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

_DEFAULT_STORE = Path(os.environ.get("HERMES_MFA_DIR", "/var/lib/hermes/mfa"))
_SECRET_FILE = "totp.json"
_STEP = 30
_DIGITS = 6
_WINDOW = 1  # accept current ±1 step (clock skew tolerance)


class ProtectionLevel(StrEnum):
    """How much owner proof an elevation requires, by action delicacy."""

    MFA = "mfa"                    # normal: TOTP code
    MFA_HUMANITY = "mfa_humanity"  # delicate: TOTP + prove-humanity challenge
    MFA_RIDDLE = "mfa_riddle"      # most delicate: TOTP + enrolled human riddle


# ---------------------------------------------------------------------------
# RFC 6238 TOTP (stdlib only)
# ---------------------------------------------------------------------------

def _b32_key(secret_b32: str) -> bytes:
    pad = "=" * (-len(secret_b32) % 8)
    return base64.b32decode(secret_b32.upper() + pad, casefold=True)


def _hotp(secret_b32: str, counter: int, digits: int = _DIGITS) -> str:
    h = hmac.new(_b32_key(secret_b32), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = (struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


def totp_now(secret_b32: str, *, at: float | None = None) -> str:
    """Current TOTP code (for tests / display)."""
    now = time.time() if at is None else at
    return _hotp(secret_b32, int(now // _STEP))


def verify_totp_counter(secret_b32: str, code: str, *, at: float | None = None) -> int | None:
    """Verify a TOTP code; return the matched step counter, or None.

    Returning the counter lets the caller enforce single-use (reject any counter
    <= the last consumed one) — RFC 6238 §5.2. Constant-time, fail-closed.
    """
    if not isinstance(code, str) or not code.strip().isdigit():
        return None
    code = code.strip().zfill(_DIGITS)
    now = time.time() if at is None else at
    current = int(now // _STEP)
    matched = -1
    for w in range(-_WINDOW, _WINDOW + 1):
        # iterate the whole window (no early return) to avoid timing leaks
        if hmac.compare_digest(_hotp(secret_b32, current + w), code):
            matched = current + w
    return matched if matched >= 0 else None


def verify_totp(secret_b32: str, code: str, *, at: float | None = None) -> bool:
    """Constant-time verify a TOTP code within the clock-skew window. Fail-closed.

    NOTE: stateless — does NOT protect against replay within the window. Use
    MfaStore.verify (which tracks the last consumed counter) for single-use.
    """
    return verify_totp_counter(secret_b32, code, at=at) is not None


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def otpauth_uri(secret_b32: str, *, label: str = "owner", issuer: str = "Lumen") -> str:
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(label)}"
        f"?secret={secret_b32}&issuer={quote(issuer)}&algorithm=SHA1&digits={_DIGITS}&period={_STEP}"
    )


# ---------------------------------------------------------------------------
# Owner-only secret store (0600, agent-inaccessible)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MfaState:
    enrolled: bool
    riddle_question: str | None = None


class MfaStore:
    """Persists the owner's TOTP secret + optional riddle in an owner-only 0600 file.

    The agent cannot read this: the file lives under /var/lib/hermes (the daemon's
    state dir, in the sandbox launchers' InaccessiblePaths and never mounted into the
    OpenShell sandbox where the agent runs).
    """

    def __init__(self, store_dir: Path = _DEFAULT_STORE) -> None:
        self._dir = store_dir
        self._path = store_dir / _SECRET_FILE

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def is_enrolled(self) -> bool:
        return bool(self._load().get("totp_secret"))

    def state(self) -> MfaState:
        d = self._load()
        return MfaState(enrolled=bool(d.get("totp_secret")), riddle_question=d.get("riddle_q"))

    def enroll(self) -> tuple[str, str]:
        """Generate + persist a fresh TOTP secret.

        Returns (otpauth_uri, secret_b32) so callers can expose the raw secret
        for manual entry without regex-parsing the URI.
        """
        secret = generate_secret()
        d = self._load()
        d["totp_secret"] = secret
        self._save(d)
        return otpauth_uri(secret), secret

    def set_riddle(self, question: str, answer: str) -> None:
        d = self._load()
        d["riddle_q"] = question.strip()
        # store only a salted hash of the answer, never the plaintext
        salt = secrets.token_hex(16)
        d["riddle_salt"] = salt
        d["riddle_hash"] = hashlib.sha256((salt + answer.strip().lower()).encode()).hexdigest()
        self._save(d)

    def verify(self, *, level: ProtectionLevel, totp: str, humanity: str | None = None,
               riddle_answer: str | None = None) -> tuple[bool, str]:
        """Verify the owner factors for the given protection level. Fail-closed.

        Returns (ok, reason). Never reveals the secret/answer.
        """
        d = self._load()
        secret = d.get("totp_secret")
        if not secret:
            return False, "mfa_not_enrolled"
        matched = verify_totp_counter(secret, totp or "")
        if matched is None:
            return False, "invalid_totp"
        # Single-use: reject any code whose step counter was already consumed
        # (replay within the ±window). Persist the high-water mark on success.
        last = d.get("totp_last_counter")
        if isinstance(last, int) and matched <= last:
            return False, "totp_replayed"
        if level is ProtectionLevel.MFA_HUMANITY:
            # the challenge text is issued+checked by the API layer; here we only
            # require that a humanity proof was supplied (the API validates it).
            if not humanity:
                return False, "humanity_required"
        if level is ProtectionLevel.MFA_RIDDLE:
            rh, rs = d.get("riddle_hash"), d.get("riddle_salt")
            if not rh or not rs:
                return False, "riddle_not_enrolled"
            got = hashlib.sha256((rs + (riddle_answer or "").strip().lower()).encode()).hexdigest()
            if not hmac.compare_digest(got, rh):
                return False, "invalid_riddle"
        # Consume the code: persist the step counter so it cannot be replayed.
        d["totp_last_counter"] = matched
        self._save(d)
        return True, "ok"
