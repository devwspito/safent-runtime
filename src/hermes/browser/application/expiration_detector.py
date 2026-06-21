"""Heuristic detector for remote-session expiration.

When a StorageState is restored but the remote site has invalidated the
session, the browser lands on a login page instead of the expected private
route.  This module detects that condition so BrowserSession can emit
OperatorReauthRequired rather than silently failing mid-flow.

Heuristics (all must be satisfied for True):
  1. URL contains a login-pattern path (``/login``, ``/auth``, ``/signin``).
  2. DOM contains an ``<input type="password">`` inside a form.

Ambiguous cases (modal with password field but no login URL) return False
with a structured warning log — autologin is never attempted (FR-005).

Constitution IV: fail-closed — when ambiguous, return False (do not
trigger reauth on a false positive, but never attempt autologin).
FR-005 / SC-006: no autologin path exists in this module.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

logger = logging.getLogger(__name__)

# Login-pattern URL fragments (case-insensitive).
_LOGIN_URL_RE = re.compile(r"/login|/auth|/signin|/acceso|/inicio-sesion", re.IGNORECASE)

# HTML: <input ... type="password" ...> (permissive attribute order).
_PASSWORD_INPUT_RE = re.compile(
    r"<input[^>]*type=['\"]password['\"]",
    re.IGNORECASE | re.DOTALL,
)

# Expiration tokens in DOM text (case-insensitive).
_EXPIRATION_TOKENS_RE = re.compile(
    r"session\s+expired|sesion\s+caducada|please\s+log\s+in\s+again"
    r"|iniciar\s+sesion|tu\s+sesion\s+ha\s+expirado",
    re.IGNORECASE,
)


def detect_expired(dom_text: str, url: str) -> bool:
    """Return True when the remote site appears to have expired the session.

    A positive result means the page is a login page reached via redirect
    after the session expired on the server side.

    False positives (modal with password, no login URL) return False with a
    warning log — callers must NOT autologin in that case either.

    Args:
        dom_text: Raw HTML of the current page (post-navigate snapshot).
        url: Current URL of the page after navigation.

    Returns:
        True if session expiration is confidently detected.
        False otherwise (ambiguous → treat as no expiration; log warning).
    """
    url_is_login = bool(_LOGIN_URL_RE.search(url))
    has_password_field = bool(_PASSWORD_INPUT_RE.search(dom_text))
    has_expiration_tokens = bool(_EXPIRATION_TOKENS_RE.search(dom_text))

    if url_is_login and has_password_field:
        logger.info(
            "hermes.browser.expiration_detector.expired",
            extra={"url": url, "has_expiration_tokens": has_expiration_tokens},
        )
        return True

    if has_password_field and not url_is_login:
        # Ambiguous: password form but URL not recognized as login.
        # Could be inline password change widget or a modal — do NOT trigger.
        logger.warning(
            "hermes.browser.expiration_detector.ambiguous",
            extra={
                "url": url,
                "reason": "password_field_without_login_url",
            },
        )
        return False

    return False


# ---------------------------------------------------------------------------
# OperatorReauthRequest event (emitted by BrowserSession when expired)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperatorReauthRequest:
    """Event emitted when the runtime detects session expiration.

    Consumer (orchestrator) must surface this to the operator.
    FR-005: the runtime never attempts autologin after emitting this event.
    """

    reason: str
    session_id: UUID
    site_id: str
    emitted_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
