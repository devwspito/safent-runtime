"""PII redactor para structlog — filtro global del stack del navegador.

Backward-compat re-export: la implementación real vive en
hermes.logging_pii_filter (standalone, sin deps de browser) para que el
daemon pueda importarla sin arrastrar stagehand_driver + rich al inicio.

Uso en composition root:

    import structlog
    from hermes.browser.infrastructure.log_filter import pii_redactor

    structlog.configure(
        processors=[
            pii_redactor,
            structlog.processors.JSONRenderer(),
        ]
    )
"""

# Re-export from the standalone module to preserve the public API.
# New code should import directly from hermes.logging_pii_filter.
from hermes.logging_pii_filter import (  # noqa: F401
    _SENSITIVE_KEYS,
    _redact_dict,
    _redact_string,
    _redact_value,
    pii_redactor,
)
