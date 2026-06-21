"""Setup REST API — presentation layer.

Endpoints:
  POST /api/v1/setup/account   stage OS account credentials for privileged apply.

The shell-server runs as User=hermes (non-root). It cannot call chpasswd
directly. Instead it stages a validated request to a tmpfs path
(/run/hermes/setup/account-request.json, mode 0600, owner hermes).

A root-owned systemd path unit (hermes-account-apply.path) watches that file
and immediately triggers hermes-account-apply.service, which runs the
account-apply script as root. The staged file is shredded after use.

This path-activated root oneshot pattern is deliberately simple and auditable:
  - The shell-server never runs as root.
  - The privileged script re-validates all inputs before touching the OS.
  - The password is never logged, never returned, never in the environment.
  - This endpoint is strictly one-time: once /var/lib/hermes/account-applied
    exists, all further calls return 409. Password changes go through
    authenticated OS settings, not this endpoint.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Default staging path (tmpfs, not persistent across reboots).
# The setup/ subdir is mode 0700 hermes:hermes (tmpfiles: d /run/hermes/setup 0700 hermes hermes).
# Overridable via env var so tests can inject a tmp directory without
# touching the real /run hierarchy.
_DEFAULT_STAGE_DIR = Path(
    os.environ.get("HERMES_ACCOUNT_STAGE_DIR", "/run/hermes/setup")
)
_STAGE_FILENAME = "account-request.json"

# Sentinel path written by the root script after successful apply.
# Overridable so tests can inject a tmp path without touching /var/lib/hermes.
_DEFAULT_SENTINEL_FILE = Path(
    os.environ.get("HERMES_ACCOUNT_SENTINEL", "/var/lib/hermes/account-applied")
)

# Username constraints: same regex enforced in the account-apply script.
# ^[a-z] — must start with lowercase letter (POSIX / useradd requirement).
# [a-z0-9_-]{0,31} — up to 31 more chars, lowercase alnum, hyphen, underscore.
# Total max 32 chars.
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_PASSWORD_MIN = 8
_PASSWORD_MAX = 256
# C0 control characters (0x00–0x1F) and DEL (0x7F) are rejected in passwords.
# chpasswd reads stdin line-by-line; a newline injects a second 'user:pass' entry.
_CTRL_LOWER = 0x20  # first non-control char (space)
_CTRL_DEL = 0x7F


class AccountRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=_PASSWORD_MIN, max_length=_PASSWORD_MAX)


class AccountStagedResponse(BaseModel):
    staged: bool


def _validate_username(username: str) -> bool:
    return bool(_USERNAME_RE.match(username))


def _validate_password(password: str) -> bool:
    """Return True only when password is safe to pass to chpasswd via stdin.

    chpasswd reads stdin as 'user:pass\\n' — a newline in the password would
    inject a second entry and could allow arbitrary account takeover.  We
    reject every C0/DEL control character (ord < 0x20 or == 0x7f) here and
    again in the root script (defence-in-depth).
    """
    if not (_PASSWORD_MIN <= len(password) <= _PASSWORD_MAX):
        return False
    return not any(ord(c) < _CTRL_LOWER or ord(c) == _CTRL_DEL for c in password)


def _stage_dir(override: Path | None) -> Path:
    return override if override is not None else _DEFAULT_STAGE_DIR


def _sentinel_file(override: Path | None) -> Path:
    return override if override is not None else _DEFAULT_SENTINEL_FILE


def create_setup_router(
    *,
    stage_dir: Path | None = None,
    sentinel_file: Path | None = None,
) -> APIRouter:
    """Create the /api/v1/setup router.

    stage_dir     — override the staging directory (for tests).
    sentinel_file — override the sentinel path (for tests).
    """
    router = APIRouter(prefix="/api/v1/setup", tags=["setup"])

    @router.post("/account", response_model=AccountStagedResponse)
    async def stage_account(payload: AccountRequest) -> AccountStagedResponse:
        """Stage OS account credentials for privileged application.

        Defence-in-depth validation runs here even though the frontend also
        validates — we are a trust boundary.

        The password is NEVER logged, never returned in the response.
        Strictly one-time: returns 409 if the sentinel already exists.
        """
        effective_sentinel = _sentinel_file(sentinel_file)
        if effective_sentinel.exists():
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "already_configured",
                    "message": (
                        "Account has already been configured. "
                        "Use OS settings to change the password."
                    ),
                },
            )

        if not _validate_username(payload.username):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_username",
                    "message": (
                        "Username must start with a lowercase letter and contain "
                        "only lowercase letters, digits, hyphens, or underscores "
                        "(max 32 chars)."
                    ),
                },
            )

        if not _validate_password(payload.password):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_password",
                    "message": (
                        "Password contains disallowed characters "
                        "(control characters are not permitted)."
                    ),
                },
            )

        effective_stage_dir = _stage_dir(stage_dir)
        _write_staged_request(
            username=payload.username,
            password=payload.password,
            stage_dir=effective_stage_dir,
        )

        logger.info(
            "hermes.setup.account.staged",
            extra={"username": payload.username},
        )
        return AccountStagedResponse(staged=True)

    return router


def _write_staged_request(
    *,
    username: str,
    password: str,
    stage_dir: Path,
) -> None:
    """Write the staged request JSON to stage_dir with mode 0600.

    The parent /run/hermes (0750) is created by systemd-tmpfiles.
    The setup/ subdir (0700 hermes:hermes) is also created by tmpfiles.
    In tests, the caller supplies a tmp_path; we create it when absent so
    tests do not need to pre-create the full hierarchy.
    We do NOT create the parent /run/hermes — if absent in production it
    means OS configuration is wrong and we fail loudly.
    """
    stage_dir.mkdir(mode=0o700, exist_ok=True)
    target = stage_dir / _STAGE_FILENAME

    payload = {
        "username": username,
        "password": password,
        "requested_at": datetime.now(tz=UTC).isoformat(),
    }

    # Write atomically: write to a temp file, chmod, then rename.
    # This avoids a window where the file exists with wrong permissions.
    tmp_path = target.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        tmp_path.rename(target)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
