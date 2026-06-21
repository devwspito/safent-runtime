"""TwoFaCaptchaDetector: detección de 2FA y CAPTCHA en el DOM.

T812 — US6/Phase 8.

Detecta heurísticamente la presencia de campos 2FA/OTP y widgets CAPTCHA
en el texto DOM serializado. Nunca intenta resolver; solo emite
OperatorInterventionRequest (Constitución IV + SC-013).

Security review (T815 / SC-013):
  - SOLO detecta. NUNCA bypass automático (Constitución IV / FR-021).
  - El caller (BrowserSession / DiscoveryRunner) recibe el
    OperatorInterventionRequest y lo escala al HitlLoop.
  - Las heurísticas son conservadoras (falsos positivos aceptables;
    falsos negativos inaceptables — es mejor pausar de más que intentar
    resolver un CAPTCHA automáticamente).
  - El detector opera sobre texto DOM serializado (no sobre el DOM vivo).
    No ejecuta ninguna acción en el browser context.

Verdict: APPROVE inline (T815).
"""

from __future__ import annotations

import re
from uuid import UUID

from hermes.browser.application.self_healing import (
    InterventionReason,
    OperatorInterventionRequest,
)

# ---------------------------------------------------------------------------
# 2FA / OTP heurísticas
# ---------------------------------------------------------------------------

# Heurísticas HTML para input de código OTP/2FA
_TWO_FA_PATTERNS: list[re.Pattern[str]] = [
    # <input type="tel" autocomplete="one-time-code">
    re.compile(
        r'<input[^>]+type=["\']tel["\'][^>]+autocomplete=["\']one-time-code["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<input[^>]+autocomplete=["\']one-time-code["\'][^>]+type=["\']tel["\']',
        re.IGNORECASE,
    ),
    # <input name*="code" maxlength="6"> (campo corto de dígitos)
    re.compile(
        r'<input[^>]+name=["\'][^"\']*code[^"\']*["\'][^>]+maxlength=["\']6["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<input[^>]+maxlength=["\']6["\'][^>]+name=["\'][^"\']*code[^"\']*["\']',
        re.IGNORECASE,
    ),
    # <input inputmode="numeric" autocomplete="one-time-code">
    re.compile(
        r'<input[^>]+inputmode=["\']numeric["\'][^>]+autocomplete=["\']one-time-code["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<input[^>]+autocomplete=["\']one-time-code["\'][^>]+inputmode=["\']numeric["\']',
        re.IGNORECASE,
    ),
]

# ---------------------------------------------------------------------------
# CAPTCHA heurísticas
# ---------------------------------------------------------------------------

# Proveedores conocidos
_CAPTCHA_IFRAME_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "recaptcha",
        re.compile(r'<iframe[^>]+src=["\'][^"\']*recaptcha[^"\']*["\']', re.IGNORECASE),
    ),
    (
        "hcaptcha",
        re.compile(r'<iframe[^>]+src=["\'][^"\']*hcaptcha[^"\']*["\']', re.IGNORECASE),
    ),
    (
        "turnstile",
        re.compile(r'<iframe[^>]+src=["\'][^"\']*turnstile[^"\']*["\']', re.IGNORECASE),
    ),
    (
        "arkoselabs",
        re.compile(
            r'<iframe[^>]+src=["\'][^"\']*arkoselabs[^"\']*["\']', re.IGNORECASE
        ),
    ),
]

_CAPTCHA_DIV_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "recaptcha",
        re.compile(
            r'<div[^>]+class=["\'][^"\']*g-recaptcha[^"\']*["\']', re.IGNORECASE
        ),
    ),
    (
        "hcaptcha",
        re.compile(
            r'<div[^>]+class=["\'][^"\']*h-captcha[^"\']*["\']', re.IGNORECASE
        ),
    ),
    (
        "turnstile",
        re.compile(
            r'<div[^>]+class=["\'][^"\']*cf-turnstile[^"\']*["\']', re.IGNORECASE
        ),
    ),
    (
        "funcaptcha",
        re.compile(
            r'<div[^>]+class=["\'][^"\']*funcaptcha[^"\']*["\']', re.IGNORECASE
        ),
    ),
]


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def detect_two_fa(dom_text: str) -> bool:
    """Detecta presencia de campo 2FA/OTP en el DOM serializado.

    Returns True si se detecta algún patrón de campo 2FA/OTP.
    Nunca lanza excepciones; si el DOM está malformado, devuelve False.
    """
    return any(pattern.search(dom_text) for pattern in _TWO_FA_PATTERNS)


def detect_captcha(dom_text: str) -> str | None:
    """Detecta presencia de CAPTCHA en el DOM serializado.

    Returns el nombre del proveedor ("recaptcha", "hcaptcha", "turnstile",
    "arkoselabs", "funcaptcha") o None si no se detecta ninguno.
    Nunca lanza excepciones.
    """
    for provider, pattern in _CAPTCHA_IFRAME_PATTERNS:
        if pattern.search(dom_text):
            return provider
    for provider, pattern in _CAPTCHA_DIV_PATTERNS:
        if pattern.search(dom_text):
            return provider
    return None


def detect_and_request_intervention(
    dom_text: str,
    *,
    session_id: UUID,
    step_id: str,
    site_id: str = "",
    flow_id: str = "",
) -> OperatorInterventionRequest | None:
    """Detecta 2FA o CAPTCHA y emite OperatorInterventionRequest etiquetada.

    Constitución IV: fail-closed. Si se detecta 2FA o CAPTCHA, SIEMPRE emite
    la solicitud de intervención. Nunca intenta resolver automáticamente.

    Args:
        dom_text: texto DOM serializado del step actual.
        session_id: UUID de la sesión activa.
        step_id: identificador del step que activó la detección.
        site_id: ID del sitio (para el envelope de intervención).
        flow_id: ID del flow (para el envelope de intervención).

    Returns:
        OperatorInterventionRequest si se detecta 2FA o CAPTCHA, None si no.
    """
    if detect_two_fa(dom_text):
        return OperatorInterventionRequest(
            reason=InterventionReason.TWO_FA_CODE,
            site_id=site_id,
            flow_id=flow_id,
            step_id=step_id,
            metadata={"session_id": str(session_id)},
        )

    captcha_provider = detect_captcha(dom_text)
    if captcha_provider is not None:
        return OperatorInterventionRequest(
            reason=InterventionReason.CAPTCHA,
            site_id=site_id,
            flow_id=flow_id,
            step_id=step_id,
            metadata={
                "session_id": str(session_id),
                "captcha_provider": captcha_provider,
            },
        )

    return None
