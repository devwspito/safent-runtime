"""Structured logging configuration for Hermes services (finding #27).

Call ``configure_structured_logging(service=..., version=...)`` from
each service entrypoint (shell-server main, whisper-worker main,
runtime main, gtk4 app main).

Processors chain:
  1. merge_contextvars      — injects trace_id / request_id bound per-request
  2. pii_redactor           — second-line-of-defense: redacts NIF/IBAN/email/tel
  3. add_log_level          — adds "level" key
  4. TimeStamper            — ISO timestamp
  5. JSONRenderer           — final JSON output

Falls back to plain ``logging.basicConfig`` if structlog is not installed,
so CI without structlog does not break.
"""

from __future__ import annotations

import logging
import sys


def configure_structured_logging(
    *,
    service: str,
    version: str,
    level: int = logging.INFO,
) -> None:
    """Install structured JSON logging on the root logger.

    Safe to call multiple times (idempotent after first call).
    """
    try:
        _configure_structlog(service=service, version=version, level=level)
    except ImportError:
        _configure_fallback(service=service, level=level)


def _configure_structlog(*, service: str, version: str, level: int) -> None:
    import structlog  # noqa: PLC0415

    # Import pii_redactor from the standalone module (hermes.logging_pii_filter)
    # to avoid pulling hermes.browser.infrastructure.__init__, which eagerly
    # imports stagehand_driver + structlog.dev + rich (~170ms cold start).
    # hermes.browser.infrastructure.log_filter re-exports pii_redactor from
    # hermes.logging_pii_filter for backward compatibility.
    try:
        from hermes.logging_pii_filter import pii_redactor  # noqa: PLC0415
    except ImportError:
        pii_redactor = None  # type: ignore[assignment]

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if pii_redactor is not None:
        # Insert before renderer so PII is stripped even in error messages.
        processors.insert(1, pii_redactor)

    processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    # Ensure stdlib handlers emit to stdout too (for uvicorn / systemd capture).
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(message)s",
    )
    logging.getLogger("hermes").setLevel(level)
    logging.getLogger(service).info(
        "structured_logging_configured",
        extra={"service": service, "version": version},
    )


def _configure_fallback(*, service: str, level: int) -> None:
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    logging.getLogger(service).warning(
        "structlog not available — falling back to plain text logging"
    )
