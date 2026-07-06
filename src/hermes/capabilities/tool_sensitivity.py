"""SENSITIVE tier — classification layer for Enterprise approval routing.

Enterprise governance, Fase 2 Phase 4a. Runtime-only. This module PRODUCES a
classification signal (`sensitivity()`); since Fase 2 Phase 4c,
`hermes.capabilities.approval_router.route()` no longer consults it for the
routing decision (routing is keyed purely on `tool_delicacy.is_mfa_required`)
— `security_hook._compute_danger_route` still calls `sensitivity()` and
persists the result as CONTEXT on an ENTERPRISE-routed pending row, pushed to
the cloud admin via `hermes.config_sync.remote_approvals` for their review.

Do NOT reinvent PII/taint detection — this module DELEGATES to the existing
single sources of truth:
  - hermes.capabilities.domain.provenance_taint (sensitive-path read detection)
  - hermes.capabilities.infrastructure.sqlite_approval_gate (PII placeholder regex)

Pure, no I/O, zero framework deps. Fail-soft: any classification error collapses
to an empty set — the caller (security_hook) still applies delicacy()/hardline
independently, so a bug here can only under-classify, never crash or over-block.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from hermes.capabilities.domain.provenance_taint import (
    ProvenanceTaint,
    is_sensitive_path_read_under_taint,
)
from hermes.runtime.nous_tool_risk_map import NousRisk, classify_nous_tool


class SensitivityCategory(StrEnum):
    """A SENSITIVE-tier signal carried by a single tool call.

    Each category is an independent, hand-curated classification axis — a
    call may carry zero, one, or several at once. Since Fase 2 Phase 4c,
    approval_router.route() no longer consults this classification (routing
    is keyed purely on tool_delicacy.is_mfa_required); the resulting set is
    still computed and persisted as CONTEXT on an ENTERPRISE-routed row, for
    the remote admin reviewing the decision.
    """

    PII_READ = "pii_read"      # reads personally-identifiable data
    NEW_EGRESS = "new_egress"  # would reach a domain outside the owner's grant
    SPEND = "spend"            # moves/authorizes money on the owner's behalf


# Domain-bearing arg keys, checked in priority order — the first present,
# non-empty value wins. Defensive: an unknown/odd shape never crashes the
# extraction, it just yields no domain (=> no NEW_EGRESS).
_EGRESS_TARGET_ARG_KEYS: tuple[str, ...] = ("url", "domain", "host")

# Hand-curated (NEVER a keyword/substring scan of tool names or args — see
# feedback_no_deterministic_routing): native tools whose PRIMARY contract
# reaches an external network target. Extend this set as new egress-capable
# native tools are added; do not pattern-match.
_EGRESS_CAPABLE_TOOLS: frozenset[str] = frozenset({
    "browser_navigate", "browser_cdp", "web_search", "web_extract",
})

# Hand-curated (NEVER a keyword/substring scan): Composio payment/checkout/
# send-money action IDs. STARTER CONTRACT for Phase 4a — this phase is
# classification-only (nothing is gated on it yet). Validate the exact
# Composio action slugs with security-engineer and extend as real payment
# toolkits are connected, before any later phase gates on this set.
_SPEND_TOOLS: frozenset[str] = frozenset({
    "STRIPE_CREATE_PAYMENT_LINK",
    "STRIPE_CREATE_CHECKOUT_SESSION",
    "STRIPE_CREATE_REFUND",
    "PAYPAL_CREATE_ORDER",
    "PAYPAL_CREATE_PAYOUT",
})


def sensitivity(
    tool_name: str,
    args: dict[str, Any],
    *,
    egress_allowlist: frozenset[str] = frozenset(),
) -> frozenset[SensitivityCategory]:
    """Classify the SENSITIVE-tier categories carried by one tool call.

    Pure, no I/O. Fail-soft: any unexpected classification error collapses
    the result to an empty set rather than raising.
    """
    try:
        safe_args = args if isinstance(args, dict) else {}
        categories: set[SensitivityCategory] = set()
        if _is_pii_read(tool_name, safe_args):
            categories.add(SensitivityCategory.PII_READ)
        if _is_new_egress(tool_name, safe_args, egress_allowlist):
            categories.add(SensitivityCategory.NEW_EGRESS)
        if tool_name in _SPEND_TOOLS:
            categories.add(SensitivityCategory.SPEND)
        return frozenset(categories)
    except Exception:  # noqa: BLE001 — fail-soft: classification error => empty set
        return frozenset()


def _is_pii_read(tool_name: str, args: dict[str, Any]) -> bool:
    """PII_READ: a native READ tool whose args reference a sensitive path
    (delegates to provenance_taint.is_sensitive_path_read_under_taint) or
    carry a `<PII:...>` placeholder (delegates to the approval-gate's regex).
    """
    try:
        if classify_nous_tool(tool_name) is not NousRisk.READ:
            return False
        taint = ProvenanceTaint(derived_from_untrusted_content=True)
        if is_sensitive_path_read_under_taint(taint, tool_name, args):
            return True
        return _contains_pii_placeholder(args)
    except Exception:  # noqa: BLE001 — fail-soft: classification error => not PII_READ
        return False


def _contains_pii_placeholder(value: Any) -> bool:  # noqa: ANN401
    """True if *value* (or anything nested inside it) matches the `<PII:...>`
    placeholder regex. Lazy import: keeps this module free of
    sqlite_approval_gate's SQLite/HMAC infrastructure import chain.
    """
    from hermes.capabilities.infrastructure.sqlite_approval_gate import (  # noqa: PLC0415
        _PII_PATTERN,
    )

    if isinstance(value, str):
        return bool(_PII_PATTERN.search(value))
    if isinstance(value, dict):
        return any(_contains_pii_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_pii_placeholder(v) for v in value)
    return False


def _is_new_egress(
    tool_name: str, args: dict[str, Any], egress_allowlist: frozenset[str]
) -> bool:
    """NEW_EGRESS: an egress-capable tool targeting a domain outside the grant."""
    try:
        if tool_name not in _EGRESS_CAPABLE_TOOLS:
            return False
        target_domain = _extract_target_domain(args)
        if target_domain is None:
            return False
        return target_domain not in egress_allowlist
    except Exception:  # noqa: BLE001 — fail-soft: extraction error => not NEW_EGRESS
        return False


def _extract_target_domain(args: dict[str, Any]) -> str | None:
    """Best-effort domain extraction from url/domain/host args.

    Defensive: any odd/malformed value yields None rather than raising.
    """
    for key in _EGRESS_TARGET_ARG_KEYS:
        raw = args.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        domain = _hostname_from(raw.strip())
        if domain:
            return domain
    return None


def _hostname_from(raw: str) -> str | None:
    try:
        candidate = raw if "//" in raw else f"//{raw}"
        hostname = urlparse(candidate).hostname
        return hostname.lower() if hostname else None
    except ValueError:
        return None
