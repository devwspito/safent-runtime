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
from copy import deepcopy
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

_HTTP_LOG_FIELDS = 5
_ASCII_SPACE = 32
_ASCII_DELETE = 127


def _http_status_phrase(status: object) -> str:
    try:
        return HTTPStatus(status).phrase if isinstance(status, int) else ""
    except ValueError:
        return ""


def _http_target_without_secrets(value: object) -> str:
    """Preserve routing metadata, not query/fragment/userinfo or control bytes."""
    try:
        parsed = urlsplit(str(value))
        path = parsed.path or "/"
        if parsed.scheme:
            host = parsed.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            port = f":{parsed.port}" if parsed.port is not None else ""
            path = f"{parsed.scheme}://{host}{port}{path}"
        return "".join(
            char if ord(char) >= _ASCII_SPACE and ord(char) != _ASCII_DELETE else "_"
            for char in path
        )
    except (TypeError, ValueError):
        return "[invalid HTTP target]"


class HttpMetadataFilter(logging.Filter):
    """Sanitize before Uvicorn formats its request tuple or HTTPX renders a URL.

    HTTPcore DEBUG payloads can include complete credential-bearing headers.
    Retain their event name/level only; the HTTPX INFO event retains the request
    method, origin, path and response status. Unknown transport messages never
    fall back to interpolating their payload.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if record.name == "uvicorn.access":
            if isinstance(args, tuple) and len(args) == _HTTP_LOG_FIELDS:
                client, method, target, version, status = args
                record.args = (
                    client,
                    method,
                    _http_target_without_secrets(target),
                    version,
                    status,
                )
            else:
                # Preserve AccessFormatter's five-field contract even for a
                # malformed/third-party event, never render its original text.
                record.args = ("unknown", "UNKNOWN", "[invalid HTTP target]", "?", 0)
            record.msg = '%s - "%s %s HTTP/%s" %d'
            record.exc_info, record.exc_text, record.stack_info = None, None, None
        elif record.name == "httpx" or record.name.startswith("httpx."):
            if (
                record.msg == 'HTTP Request: %s %s "%s %d %s"'
                and isinstance(args, tuple)
                and len(args) == _HTTP_LOG_FIELDS
            ):
                method, target, version, status, _reason = args
                record.args = (
                    method,
                    _http_target_without_secrets(target),
                    version,
                    status,
                    _http_status_phrase(status),
                )
            else:
                record.msg, record.args = "HTTP transport diagnostic (payload omitted)", ()
            record.exc_info, record.exc_text, record.stack_info = None, None, None
        elif record.name == "httpcore" or record.name.startswith("httpcore."):
            # HTTPcore's event names are fixed dotted identifiers; its values
            # and exceptions are opaque data, not safe forensic metadata.
            event = str(record.msg).split(" ", 1)[0]
            if not event or not all(
                char.isascii() and (char.isalnum() or char in "._") for char in event
            ):
                event = "transport"
            record.msg, record.args = f"HTTP transport {event} (payload omitted)", ()
            record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True


def uvicorn_log_config() -> dict[str, Any]:
    """Uvicorn-owned config: redaction survives its startup dictConfig call."""
    from uvicorn.config import LOGGING_CONFIG  # noqa: PLC0415

    config = deepcopy(LOGGING_CONFIG)
    config["filters"] = {"http_metadata": {"()": HttpMetadataFilter}}
    config["handlers"]["access"]["filters"] = ["http_metadata"]
    config["handlers"]["http_metadata"] = {
        **config["handlers"]["default"],
        "filters": ["http_metadata"],
    }
    for name in ("httpx", "httpcore"):
        config["loggers"][name] = {
            "handlers": ["http_metadata"],
            "level": "INFO",
            "propagate": False,
        }
    return config


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
