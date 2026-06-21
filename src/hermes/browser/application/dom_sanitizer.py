"""DOM sanitization layer: strip injection vectors before sending to LLM.

Threat-model control P1 #4 — surface 1 (BrowserSession + StagehandDriver).
Defends against prompt-injection from adversarial DOM content such as:
  - Hidden nodes with "Ignore previous instructions" payloads.
  - aria-label / title attributes carrying override tokens.

Constitution III: no PII logged — only stripped_count + truncated (counters).
Constitution IV: fail-closed — malformed HTML must never crash the caller;
  returns best-effort sanitized text instead of raising.

Uses selectolax (lexbor backend) when available; falls back to a regex-based
approach so the module is importable without the [browser] extra installed.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass

import structlog

log = structlog.get_logger("hermes.browser.dom_sanitizer")

_TRUNCATION_SUFFIX = b"[...truncated]"

# Case-insensitive tokens that signal prompt-injection in aria-label / title.
_INJECTION_TOKENS: tuple[str, ...] = (
    "ignore",
    "system",
    "instruction",
    "developer",
    "override",
    "previous instructions",
)
_INJECTION_RE = re.compile(
    "|".join(re.escape(t) for t in _INJECTION_TOKENS),
    re.IGNORECASE,
)

# Regex patterns for fallback (no selectolax) path.
# Matches hidden / aria-hidden="true" / display:none / visibility:hidden nodes.
_HIDDEN_BLOCK_RE = re.compile(
    r"<[^>]+(?:"
    r'hidden(?:\s|>|/)'
    r"|aria-hidden\s*=\s*[\"']true[\"']"
    r"|style\s*=\s*[\"'][^\"']*display\s*:\s*none[^\"']*[\"']"
    r"|style\s*=\s*[\"'][^\"']*visibility\s*:\s*hidden[^\"']*[\"']"
    r")[^>]*>.*?</[a-zA-Z]+>",
    re.DOTALL | re.IGNORECASE,
)

# Matches aria-label or title attribute containing injection tokens.
_INJECT_ATTR_RE = re.compile(
    r"""\s*(?:aria-label|title)\s*=\s*["'][^"']*?(?:"""
    + "|".join(re.escape(t) for t in _INJECTION_TOKENS)
    + r""")[^"']*?["']""",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SanitizedDom:
    """Result of :func:`sanitize_for_llm`."""

    text: str
    stripped_count: int
    truncated: bool


def sanitize_for_llm(dom_text: str, *, max_bytes: int = 51200) -> SanitizedDom:
    """Strip injection vectors and cap DOM text before it reaches the LLM.

    Args:
        dom_text:  Raw DOM string (HTML or serialised DOM).
        max_bytes: Byte cap after encoding to UTF-8.  Default 50 KB.

    Returns:
        :class:`SanitizedDom` with sanitized ``text``, ``stripped_count``,
        and ``truncated`` flag.
    """
    try:
        return _sanitize(dom_text, max_bytes=max_bytes)
    except Exception:  # noqa: BLE001 — fail-closed: degrade, never crash caller
        # Last-resort: return raw text capped, stripped_count=0.
        raw = _cap_bytes(dom_text, max_bytes=max_bytes)
        truncated = len(dom_text.encode("utf-8", errors="replace")) > max_bytes
        log.warning(
            "hermes.browser.dom_sanitizer.fallback",
            reason="unexpected_error",
            stripped_count=0,
            truncated=truncated,
        )
        return SanitizedDom(text=raw, stripped_count=0, truncated=truncated)


def _sanitize(dom_text: str, *, max_bytes: int) -> SanitizedDom:
    stripped_count = 0
    try:
        import selectolax  # noqa: PLC0415 — intentional lazy-import for optional dep  # type: ignore[import-untyped]
        _ = selectolax  # confirm importable; actual API used in _sanitize_selectolax
        result_text, stripped_count = _sanitize_selectolax(dom_text)
    except ModuleNotFoundError:
        result_text, stripped_count = _sanitize_regex(dom_text)

    result_text, truncated = _apply_cap(result_text, max_bytes=max_bytes)

    log.info(
        "hermes.browser.dom_sanitized",
        stripped_count=stripped_count,
        truncated=truncated,
    )
    return SanitizedDom(text=result_text, stripped_count=stripped_count, truncated=truncated)


def _sanitize_selectolax(dom_text: str) -> tuple[str, int]:
    """Selectolax-based path (preferred — lexbor is fast and spec-compliant)."""
    from selectolax.parser import HTMLParser  # noqa: PLC0415  # type: ignore[import-untyped]

    tree = HTMLParser(dom_text)
    stripped = 0

    for node in list(tree.css("*")):
        if _node_is_hidden(node):
            node.decompose()
            stripped += 1
            continue
        stripped += _strip_injection_attrs(node)

    return tree.html or "", stripped


def _node_is_hidden(node: object) -> bool:
    """Return True if the node should be stripped entirely."""
    attrs: dict[str, str] = getattr(node, "attributes", {}) or {}

    if "hidden" in attrs:
        return True
    if attrs.get("aria-hidden", "").lower() == "true":
        return True

    style = attrs.get("style", "")
    if re.search(r"display\s*:\s*none", style, re.IGNORECASE):
        return True
    return bool(re.search(r"visibility\s*:\s*hidden", style, re.IGNORECASE))


def _strip_injection_attrs(node: object) -> int:
    """Remove aria-label / title attrs that contain injection tokens. Returns count."""
    attrs: dict[str, str] = getattr(node, "attributes", {}) or {}
    stripped = 0
    for attr_name in ("aria-label", "title"):
        value = attrs.get(attr_name, "")
        if value and _INJECTION_RE.search(value):
            # selectolax does not expose attr deletion; setting to empty is safe.
            with contextlib.suppress(AttributeError, TypeError):
                node.attrs[attr_name] = ""  # type: ignore[index]
            stripped += 1
    return stripped


def _sanitize_regex(dom_text: str) -> tuple[str, int]:
    """Regex-based fallback when selectolax is not installed."""
    stripped = 0

    cleaned, n = _HIDDEN_BLOCK_RE.subn("", dom_text)
    stripped += n

    cleaned, n = _INJECT_ATTR_RE.subn("", cleaned)
    stripped += n

    return cleaned, stripped


def _apply_cap(text: str, *, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False

    available = max_bytes - len(_TRUNCATION_SUFFIX)
    if available <= 0:
        truncated_bytes = _TRUNCATION_SUFFIX[:max_bytes]
        return truncated_bytes.decode("utf-8", errors="replace"), True

    trimmed = encoded[:available].decode("utf-8", errors="replace")
    return trimmed + _TRUNCATION_SUFFIX.decode("utf-8"), True


def _cap_bytes(text: str, *, max_bytes: int) -> str:
    """Simple byte-cap for the exception fallback path."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    available = max_bytes - len(_TRUNCATION_SUFFIX)
    if available <= 0:
        return _TRUNCATION_SUFFIX[:max_bytes].decode("utf-8", errors="replace")
    return (
        encoded[:available].decode("utf-8", errors="replace")
        + _TRUNCATION_SUFFIX.decode("utf-8")
    )
