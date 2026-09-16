"""Interpret Hermes 0.21.1's structured terminal result, never its prose.

The native SDK returns error/interruption dictionaries rather than raising on
several paths. ``completed`` alone is insufficient: its finalizer may set it
true alongside ``interrupted``. Pending proposals come from our broker, not SDK
or model-supplied flags, and cannot turn a failed turn into successful execution.
"""

from typing import Literal

from hermes.domain.reasoning_failure import NativeTurnFailedError
from hermes.tasks.domain.task_cancel_registry import OperationCancelled

_FAILURE_CODES = {
    "auth": "auth",
    "auth_permanent": "auth",
    "billing": "billing",
    "rate_limit": "rate_limit",
    "upstream_rate_limit": "rate_limit",
    "overloaded": "unavailable",
    "server_error": "unavailable",
    "timeout": "timeout",
    "ssl_cert_verification": "tls",
    "context_overflow": "request",
    "payload_too_large": "request",
    "image_too_large": "request",
    "image_corrupt": "request",
    "model_not_found": "request",
    "format_error": "request",
    "provider_policy_blocked": "policy",
    "content_policy_blocked": "policy",
}


def classify_native_result(
    result: object,
    *,
    has_pending_proposals: bool = False,
) -> Literal["completed", "pending_approval"]:
    if not isinstance(result, dict):
        raise NativeTurnFailedError("invalid_result")
    if result.get("final_response") is not None and not isinstance(result["final_response"], str):
        raise NativeTurnFailedError("invalid_result")
    for flag in ("completed", "failed", "interrupted", "partial", "failure_retryable"):
        if flag in result and type(result[flag]) is not bool:
            raise NativeTurnFailedError("invalid_result")
    if result.get("interrupted") is True:
        raise OperationCancelled("La ejecución fue interrumpida antes de completarse.")
    if result.get("failed") is True or result.get("error") not in (None, ""):
        reason = result.get("failure_reason")
        code = _FAILURE_CODES.get(reason, "failed") if isinstance(reason, str) else "failed"
        retryable = result.get("failure_retryable") is True and code not in {
            "auth",
            "billing",
            "tls",
            "request",
            "policy",
        }
        raise NativeTurnFailedError(code, retryable=retryable)
    if "completed" not in result:
        # All inspected native success paths carry a real boolean. Missing it
        # is not a legacy success format; do not guess from nonempty prose.
        raise NativeTurnFailedError("invalid_result")
    if result.get("partial") is True:
        raise NativeTurnFailedError("incomplete")
    if result["completed"] is not True:
        if has_pending_proposals:
            return "pending_approval"
        raise NativeTurnFailedError("incomplete")
    return "completed"
