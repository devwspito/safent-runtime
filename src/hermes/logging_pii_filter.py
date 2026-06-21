"""PII redactor para structlog — standalone, sin dependencias de browser.

Mismo contrato que hermes.browser.infrastructure.log_filter.pii_redactor
pero sin arrastrar hermes.browser.infrastructure (stagehand_driver, signed_
selector_registry, dom_sanitizer…) al arrancar el daemon.

Constitución III: PII jamás en logs. Ninguna excepción.

Por qué se extrajo aquí:
  hermes.browser.infrastructure.__init__ importa eagerly stagehand_driver y
  friends, que arrastran structlog.dev + rich (~170ms en cold start). Importar
  pii_redactor desde ese paquete bloqueaba READY=1 innecesariamente.
  Este módulo tiene zero deps de browser y se puede importar en <1ms.

Compatibilidad:
  hermes.browser.infrastructure.log_filter.pii_redactor sigue funcionando —
  ese módulo re-exporta desde aquí.  No hay cambio en la API pública.
"""

from __future__ import annotations

import re
from typing import Any

# Claves cuyo valor se redacta completamente independientemente de su valor.
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "nif",
        "iban",
        "email",
        "password",
        "token",
        "dni",
        "cuenta",
        "credit_card",
        "ssn",
        "cif",
        "passport",
        "tarjeta",
        "secret",
        "api_key",
        "access_token",
        "refresh_token",
        "authorization",
        "x-api-key",
    }
)

# Patrones de PII en texto libre.
_NIF_RE = re.compile(r"\b\d{8}[A-Z]\b")
_NIE_RE = re.compile(r"\b[XYZ]\d{7}[A-Z]\b")
_IBAN_ES_RE = re.compile(r"\bES\d{2}[ ]?\d{4}[ ]?\d{4}[ ]?\d{2}[ ]?\d{10}\b")
_IBAN_GENERIC_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


def pii_redactor(
    logger: object,  # noqa: ARG001
    method_name: str,  # noqa: ARG001
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor que redacta PII del event_dict.

    Combina:
    - Redacción por nombre de clave (SENSITIVE_KEYS).
    - Redacción por regex en valores de tipo str.

    Recursivo en dicts y listas.
    """
    return {
        k: (
            "<<REDACTED>>"
            if k.lower() in _SENSITIVE_KEYS
            else _redact_value(v)
        )
        for k, v in event_dict.items()
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return _redact_dict(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    return value


def _redact_string(text: str) -> str:
    result = _NIF_RE.sub("<<NIF_REDACTED>>", text)
    result = _NIE_RE.sub("<<NIE_REDACTED>>", result)
    result = _IBAN_ES_RE.sub("<<IBAN_REDACTED>>", result)
    return _EMAIL_RE.sub("<<EMAIL_REDACTED>>", result)


def _redact_dict(d: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (
            "<<REDACTED>>"
            if k.lower() in _SENSITIVE_KEYS
            else _redact_value(v)
        )
        for k, v in d.items()
    }
