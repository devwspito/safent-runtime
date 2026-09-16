"""Community owner confirmation, without MFA or an agent-authorizable fallback.

The owner's UI session is distinct from the internal daemon bearer. A recorded
decision yields a short-lived, one-use capability for the exact follow-up action.
Storage is per application process (the shell runs one worker); a restart burns
all outstanding grants. It is not a replacement for Enterprise authorization.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

_TTL_SECONDS = 120
_MAX_PENDING = 1024
_LOCK = threading.Lock()


@dataclass(frozen=True)
class _Grant:
    owner: str
    identifier: str
    action: str
    expires_at: float


def require_owner_session(request: Request) -> str:
    """Authenticate the UI owner, never the internal daemon/operator credential."""
    expected = getattr(request.app.state, "shell_webui_token", "")
    authorization = request.headers.get("authorization", "")
    token = authorization[7:] if authorization[:7].lower() == "bearer " else ""
    if not expected or not token or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "owner_session_required",
                "message": "Confirma desde tu sesión de Safent.",
            },
        )
    return hashlib.sha256(token.encode()).hexdigest()


def issue_owner_approval(request: Request, *, identifier: str, action: str) -> str:
    """Only call AFTER the runtime has successfully recorded the owner decision."""
    owner = require_owner_session(request)
    now = time.monotonic()
    with _LOCK:
        grants = getattr(request.app.state, "owner_approval_grants", {})
        grants = {key: value for key, value in grants.items() if value.expires_at > now}
        request.app.state.owner_approval_grants = grants
        if len(grants) >= _MAX_PENDING:
            raise HTTPException(status_code=429, detail={"code": "too_many_pending_approvals"})
        token = secrets.token_urlsafe(32)
        grants[token] = _Grant(owner, identifier, action, now + _TTL_SECONDS)
        return token


def require_owner_approval(request: Request, *, identifier: str, action: str) -> None:
    owner = require_owner_session(request)
    token = request.headers.get("x-owner-approval-grant", "")
    with _LOCK:
        grants = getattr(request.app.state, "owner_approval_grants", {})
        grant = grants.pop(token, None)
    # A mismatch also burns the grant. No TOTP, bearer-only or force=True fallback.
    if (
        grant is None
        or grant.owner != owner
        or grant.identifier != identifier
        or grant.action != action
        or grant.expires_at <= time.monotonic()
    ):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "invalid_owner_approval",
                "message": "Revisa y confirma de nuevo esta instalación.",
            },
        )
