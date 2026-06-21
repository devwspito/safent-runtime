"""Remote-access tunnel control API.

Endpoints:
  POST /api/v1/remote-access/disable   body {password}  — consent-gated
  POST /api/v1/remote-access/enable    (no body)        — no auth
  GET  /api/v1/remote-access/status                     — {active: bool}

Design decision: password verification runs in the ROOT HELPER, not here.
Reason: the shell-server unit has `NoNewPrivileges=yes`, which blocks the
setuid bit on `unix_chkpwd`.  PAM verification via `pam_unix` requires that
setuid helper, so it would silently fail under our sandbox.  To avoid
weakening the hardening (option b: relax NoNewPrivileges — rejected), we
stage the password into the drop file and let the root oneshot do the verify
and act atomically.  This mirrors exactly how hermes-account-apply handles
credentials.

Consequence: on a wrong password the shell-server returns 202 Accepted
(staged), then the root helper shreds the file without calling systemctl.
The UI polls /status to detect whether the disable actually happened.  The
GTK layer handles the "pending → no change" case by re-checking status after
a short delay and showing an error if the state did not change.

The password is NEVER logged here.  The staged file is 0600 and is shredded
by the root helper within milliseconds of being written.

Rate limiting: the shell-server enforces a per-request-IP limit on DISABLE
attempts (5 failures / 60 s).  Because the root helper does the actual
verify, the rate limit here gates file-write frequency.  This prevents an
attacker who can POST to the shell-server from hammering the root helper
via staged files.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes.shell_server.remote_access_tunnel.rate_limiter import PasswordRateLimiter
from hermes.shell_server.remote_access_tunnel.service_status import all_services_active

logger = logging.getLogger(__name__)

_DEFAULT_CONTROL_DIR = Path(
    os.environ.get("HERMES_REMOTE_CONTROL_DIR", "/run/hermes/remote-control")
)
_REQUEST_FILENAME = "request.json"

_PASSWORD_MIN = 8
_PASSWORD_MAX = 256
_CTRL_LOWER = 0x20
_CTRL_DEL = 0x7F


# Shared rate-limiter instance — one per server process.
_rate_limiter = PasswordRateLimiter()


# --------------------------------------------------------------------------
# Pydantic schemas
# --------------------------------------------------------------------------


class DisableRequest(BaseModel):
    password: str = Field(min_length=_PASSWORD_MIN, max_length=_PASSWORD_MAX)


class RemoteAccessStatusResponse(BaseModel):
    active: bool


class RemoteAccessActionResponse(BaseModel):
    staged: bool


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _validate_password_chars(password: str) -> bool:
    """Reject C0/DEL control characters (mirrors hermes-account-apply)."""
    return not any(ord(c) < _CTRL_LOWER or ord(c) == _CTRL_DEL for c in password)


def _control_dir(override: Path | None) -> Path:
    return override if override is not None else _DEFAULT_CONTROL_DIR


def _write_staged_request(
    action: str,
    *,
    password: str | None,
    control_dir: Path,
) -> None:
    """Write the staged request JSON with mode 0600.

    The write is atomic: write to tmp → chmod → rename.
    The parent dir must already exist (created by systemd-tmpfiles).
    We do NOT create it — if absent in production, OS config is wrong.
    """
    target = control_dir / _REQUEST_FILENAME

    payload: dict = {
        "action": action,
        "requested_at": datetime.now(tz=UTC).isoformat(),
    }
    if password is not None:
        payload["password"] = password

    tmp_path = target.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        tmp_path.rename(target)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _client_key(request: Request) -> str:
    """Return a rate-limit key for the current request (client IP)."""
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------
# Router factory
# --------------------------------------------------------------------------


def create_remote_access_tunnel_router(
    *,
    control_dir: Path | None = None,
    rate_limiter: PasswordRateLimiter | None = None,
) -> APIRouter:
    """Create the /api/v1/remote-access router.

    control_dir  — override the drop directory (for tests).
    rate_limiter — override the rate-limiter instance (for tests; production
                   uses the module-level singleton so limits persist across
                   requests within the same process lifetime).
    """
    effective_limiter = rate_limiter if rate_limiter is not None else _rate_limiter
    router = APIRouter(prefix="/api/v1/remote-access", tags=["remote-access-tunnel"])

    @router.get("/status", response_model=RemoteAccessStatusResponse)
    async def get_status() -> RemoteAccessStatusResponse:
        """Return whether all three remote-access services are active."""
        return RemoteAccessStatusResponse(active=all_services_active())

    @router.post("/enable", response_model=RemoteAccessActionResponse)
    async def enable_remote_access() -> RemoteAccessActionResponse:
        """Enable the remote-access tunnel — no password required.

        Enabling is not dangerous (it does not lock anyone out); consent
        is only required to DISABLE.
        """
        effective_dir = _control_dir(control_dir)
        _write_staged_request("enable", password=None, control_dir=effective_dir)
        logger.info("hermes.remote_access.enable.staged")
        return RemoteAccessActionResponse(staged=True)

    @router.post("/disable", response_model=RemoteAccessActionResponse)
    async def disable_remote_access(
        payload: DisableRequest,
        request: Request,
    ) -> RemoteAccessActionResponse:
        """Disable the remote-access tunnel — requires device password.

        Password is staged into a 0600 drop file and verified by the root
        helper.  It is NEVER logged here.  On wrong password the root helper
        aborts without calling systemctl; the UI must poll /status to confirm.

        Rate limited: 5 failed-writes per IP per 60 seconds.
        """
        key = _client_key(request)
        if effective_limiter.is_blocked(key):
            logger.warning(
                "hermes.remote_access.disable.rate_limited",
                extra={"key": key},
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "too_many_attempts",
                    "message": "Demasiados intentos. Espera un momento e inténtalo de nuevo.",
                },
            )

        if not _validate_password_chars(payload.password):
            effective_limiter.record_failure(key)
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_password",
                    "message": "La contraseña contiene caracteres no permitidos.",
                },
            )

        effective_dir = _control_dir(control_dir)
        try:
            _write_staged_request(
                "disable",
                password=payload.password,
                control_dir=effective_dir,
            )
        except OSError as exc:
            logger.error(
                "hermes.remote_access.disable.stage_failed",
                extra={"error": str(exc)},
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "stage_failed",
                    "message": "No se pudo preparar la solicitud. Inténtalo de nuevo.",
                },
            ) from exc

        # Record a potential failure — if the root helper later rejects the
        # password, we have already counted this as a suspicious attempt.
        # This is defence-in-depth: even if the helper were somehow bypassed
        # we have counted every disable attempt toward the rate limit.
        effective_limiter.record_failure(key)

        logger.info("hermes.remote_access.disable.staged")
        return RemoteAccessActionResponse(staged=True)

    return router
