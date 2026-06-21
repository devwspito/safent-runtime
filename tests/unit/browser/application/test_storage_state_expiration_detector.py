"""T402: Tests for expiration_detector.detect_expired().

Covers US2/AC3 — remote session expiry detection.
Constitution V: pure Python, no driver, no Playwright.
FR-005: no autologin path — detector only signals True/False.
"""

from __future__ import annotations

import logging

from hermes.browser.application.expiration_detector import detect_expired

# ---------------------------------------------------------------------------
# Happy-path: definitive expiration detected
# ---------------------------------------------------------------------------


def test_password_form_plus_login_url_returns_true() -> None:
    """form[name=password] + URL /login → expired."""
    dom = '<form><input type="password" name="password" /></form>'
    url = "https://sede.aeat.es/login"
    assert detect_expired(dom, url) is True


def test_password_form_plus_auth_url_returns_true() -> None:
    """form[name=password] + URL /auth → expired."""
    dom = '<input type="password" />'
    url = "https://example.com/auth/token"
    assert detect_expired(dom, url) is True


def test_password_form_plus_signin_url_returns_true() -> None:
    """form[name=password] + URL /signin → expired."""
    dom = "<form><input TYPE='password' /></form>"
    url = "https://app.example.com/signin"
    assert detect_expired(dom, url) is True


# ---------------------------------------------------------------------------
# Ambiguous: modal with password but URL is not login
# ---------------------------------------------------------------------------


def test_password_form_without_login_url_returns_false(caplog) -> None:
    """Modal with password field but URL not login pattern → False with warning."""
    dom = '<div class="modal"><input type="password" /></div>'
    url = "https://app.example.com/dashboard"

    with caplog.at_level(logging.WARNING, logger="hermes.browser.application.expiration_detector"):
        result = detect_expired(dom, url)

    assert result is False
    # Warning must be emitted (ambiguous path).
    assert any("ambiguous" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# No password form at all
# ---------------------------------------------------------------------------


def test_dom_without_login_form_returns_false() -> None:
    """Generic DOM without password input → False regardless of URL."""
    dom = "<html><body><p>Welcome back!</p></body></html>"
    url = "https://sede.aeat.es/home"
    assert detect_expired(dom, url) is False


def test_empty_dom_returns_false() -> None:
    dom = ""
    url = ""
    assert detect_expired(dom, url) is False


# ---------------------------------------------------------------------------
# login URL but NO password form → False
# ---------------------------------------------------------------------------


def test_login_url_without_password_field_returns_false() -> None:
    """Navigating to /login route but no password form → not expired (could be landing)."""
    dom = "<html><body><p>Welcome</p></body></html>"
    url = "https://app.example.com/login"
    assert detect_expired(dom, url) is False


# ---------------------------------------------------------------------------
# Expiration token in DOM (belt-and-suspenders)
# ---------------------------------------------------------------------------


def test_expiration_token_in_dom_with_login_url_returns_true() -> None:
    dom = '<p>Session expired</p><input type="password" />'
    url = "https://example.com/auth/login"
    assert detect_expired(dom, url) is True


# ---------------------------------------------------------------------------
# FR-005 invariant: detector never autologs-in (pure boolean)
# ---------------------------------------------------------------------------


def test_detect_expired_returns_bool_only() -> None:
    """detect_expired returns plain bool — no side effects, no autologin."""
    dom = '<input type="password" />'
    url = "/login"
    result = detect_expired(dom, url)
    assert isinstance(result, bool)
