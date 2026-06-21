"""mcp/domain/tool_classifier — classify_mcp_tool() pure function.

Security invariants (plan.md §Data model):
  - Default risk = HIGH unless provably read-only.
  - readOnlyHint / destructiveHint from the MCP spec are ADVISORY:
      * readOnlyHint=True MAY lower risk to LOW (keep-safe).
      * destructiveHint=True forces HIGH regardless of other hints.
      * Neither hint can ELEVATE risk beyond what server trust allows.
  - The tool's effective trust never exceeds its server's TrustLevel.
  - auto_executable=True only for LOW risk + non-USER_ADDED trust.

Domain layer: pure Python + stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.capabilities.domain.ports import RiskLevel

from .value_objects import TrustLevel


@dataclass(frozen=True)
class McpToolClassification:
    """Output of classify_mcp_tool()."""

    risk: RiskLevel
    auto_executable: bool


# Names whose suffix pattern strongly implies read-only semantics.
# Conservative list — only verbs with zero side-effect ambiguity.
_READ_SUFFIXES: frozenset[str] = frozenset(
    {"list", "get", "fetch", "read", "search", "find", "describe", "status", "ping", "inspect"}
)


def classify_mcp_tool(
    name: str,
    *,
    read_only_hint: bool | None = None,
    destructive_hint: bool | None = None,
    trust_level: TrustLevel,
) -> McpToolClassification:
    """Classify an MCP tool's risk and auto_executable flag.

    Defaults to HIGH unless:
      - readOnlyHint is explicitly True AND destructiveHint is not True,
        AND the tool name's last segment matches a known read-only verb.

    Advisory hints may only keep-safe, never elevate risk.
    USER_ADDED servers force auto_executable=False (always require HITL).
    """
    forced_high = _is_forced_high(destructive_hint, trust_level)
    if forced_high:
        return McpToolClassification(risk=RiskLevel.HIGH, auto_executable=False)

    if read_only_hint is True and _name_looks_read_only(name):
        risk = RiskLevel.LOW
        auto_executable = trust_level is not TrustLevel.USER_ADDED
        return McpToolClassification(risk=risk, auto_executable=auto_executable)

    return McpToolClassification(risk=RiskLevel.HIGH, auto_executable=False)


def _is_forced_high(destructive_hint: bool | None, trust_level: TrustLevel) -> bool:
    if destructive_hint is True:
        return True
    # USER_ADDED is untrusted — HITL on every call per plan.md §Security model.
    if trust_level is TrustLevel.USER_ADDED:
        return True
    return False


def _name_looks_read_only(name: str) -> bool:
    """True if the last underscore-separated segment matches a read-only verb."""
    parts = name.lower().split("_")
    return bool(parts) and parts[-1] in _READ_SUFFIXES
