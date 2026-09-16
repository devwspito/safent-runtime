"""Error mapping — Composio SDK exceptions to the package's own error type.

The provider's raw response body never crosses this boundary: `ComposioApiError`
truncates every `detail` defensively so a stack trace or an error body can
never carry the API key (it wouldn't be present anyway, but nothing here
assumes that). Truncation happens ONCE, in `ComposioApiError.__init__` — the
single choke-point every caller goes through, regardless of how `detail` was
built — so `extract_detail` below does not repeat it.
"""

from __future__ import annotations

from composio_client import APIStatusError

_SAFE_DETAIL_MAX = 300


class ComposioApiError(Exception):
    """Raised when the Composio SDK call fails.

    `detail` is truncated to `_SAFE_DETAIL_MAX` chars and never contains the
    API key.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        truncated = detail[:_SAFE_DETAIL_MAX]
        super().__init__(f"Composio API {status_code}: {truncated}")
        self.status_code = status_code
        self.detail = truncated


def extract_detail(exc: APIStatusError) -> str:
    """Best-effort extraction of an error detail from an APIStatusError.

    Untruncated — `ComposioApiError.__init__` is the single truncation point.
    """
    if exc.body is not None:
        return str(exc.body)
    if hasattr(exc, "response") and exc.response is not None:
        try:
            return exc.response.text
        except Exception:  # noqa: BLE001 — best-effort only, never raise while erroring
            return str(exc)
    return ""
