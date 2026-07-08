"""mcp/domain/tool_classifier — classify_mcp_tool() pure function.

Security invariants (plan.md §Data model):
  - Default risk = HIGH unless provably read-only.
  - readOnlyHint / destructiveHint from the MCP spec are ADVISORY (for the
    BUILTIN-excluded, non-MANAGED_REMOTE tiers — i.e. USER_TRUSTED/USER_ADDED):
      * readOnlyHint=True MAY lower risk to LOW (keep-safe).
      * destructiveHint=True forces HIGH regardless of other hints.
      * Neither hint can ELEVATE risk beyond what server trust allows.
  - The tool's effective trust never exceeds its server's TrustLevel.
  - auto_executable=True only for LOW risk + non-USER_ADDED trust.
  - BUILTIN (our vetted, jailed, no-egress factory MCPs): frictionless —
      ALWAYS LOW+auto, hints ignored. The jail is the control, not approval.
  - MANAGED_REMOTE (first-party, but egressing to a managed control-plane):
      classified PURELY from the tool NAME — BOTH hints ignored, because a
      MANAGED_REMOTE server's annotations are untrusted by design and must not
      flip our verdict. Read verbs → LOW+auto (reads flow); write verbs →
      LOW+not-auto (writes gate via not-auto, and CTRL-5 forces HITL the moment
      the cycle is tainted). See _classify_managed_remote().

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
    # BUILTIN = MCP de fábrica horneado y vetado por NOSOTROS (local, sin egress, confinado
    # a la jaula). TODAS sus operaciones fluyen sin HITL (LOW + auto), incluido guardar/
    # sobre-escribir en el workspace: la JAULA es el control, no la aprobación. Solo
    # nuestros slugs sembrados llegan como BUILTIN; lo que añada el usuario es USER_ADDED →
    # gateado abajo. (Decisión del dueño: la jaula nunca debe ser un estorbo.)
    if trust_level is TrustLevel.BUILTIN:
        return McpToolClassification(risk=RiskLevel.LOW, auto_executable=True)

    # MANAGED_REMOTE: classify by NAME only, BEFORE the destructive_hint force-high.
    # A MANAGED_REMOTE server's RESPONSES/annotations are untrusted by design
    # (value_objects.TrustLevel), and a control-plane bridge (fastapi_mcp) emits
    # destructiveHint=True on every generated tool — reads included — which would
    # gate even pure reads (list_/get_). Our conservative verb-first read detection
    # is the authority: reads flow (LOW+auto), writes stay LOW+not-auto (gated).
    # An untrusted remote cannot flip our read verdict via a hint.
    if trust_level is TrustLevel.MANAGED_REMOTE:
        return _classify_managed_remote(name)

    forced_high = _is_forced_high(destructive_hint, trust_level)
    if forced_high:
        return McpToolClassification(risk=RiskLevel.HIGH, auto_executable=False)

    if read_only_hint is True and _name_looks_read_only(name):
        risk = RiskLevel.LOW
        auto_executable = trust_level is not TrustLevel.USER_ADDED
        return McpToolClassification(risk=risk, auto_executable=auto_executable)

    return McpToolClassification(risk=RiskLevel.HIGH, auto_executable=False)


def _classify_managed_remote(name: str) -> McpToolClassification:
    """Classify a MANAGED_REMOTE tool by name alone (no hint dependency).

    MANAGED_REMOTE servers are first-party but egress to a managed
    control-plane (e.g. safent-control). Reads flow fluidly: LOW + auto.
    Writes stay LOW (typed single-writes remain usable per the broker's
    autonomy table) but are NEVER auto_executable — this is deliberate,
    not a gap: it's what makes requires_forced_hitl() bite the instant the
    cycle is tainted by an untrusted MCP response (CTRL-5). Using HIGH
    here instead would force HITL on every write even when untainted,
    killing the fluency this tier exists to provide.

    BOTH readOnlyHint and destructiveHint are IGNORED for this tier: the caller
    routes MANAGED_REMOTE here BEFORE the _is_forced_high(destructive_hint) check
    precisely so an untrusted remote's annotations cannot flip our verdict —
    neither to auto-approve a write (readOnlyHint) nor to gate a read
    (destructiveHint). Writes already gate via auto_executable=False + CTRL-5, so
    ignoring destructiveHint loses no safety while preserving read fluency.
    """
    if _managed_remote_looks_read_only(name):
        return McpToolClassification(risk=RiskLevel.LOW, auto_executable=True)
    return McpToolClassification(risk=RiskLevel.LOW, auto_executable=False)


def _managed_remote_looks_read_only(name: str) -> bool:
    """True if either end of the tool name matches a read-only verb.

    Deliberately broader than _name_looks_read_only() (suffix-only): a
    control-plane bridge like safent-control mirrors REST-style resource
    endpoints, which snake_case to VERB-FIRST tool names (list_agents,
    get_usage, create_employee, delete_agent) — the opposite convention
    from the noun-first tools tested elsewhere (resource_list). Checking
    both ends against the same conservative _READ_SUFFIXES set covers
    both conventions without widening what counts as a "read verb".

    The name may arrive QUALIFIED as ``mcp__<slug>__<tool>`` (the broker classifies
    by qualified_name). Strip that prefix first — otherwise ``mcp__safent-control__
    list_employees`` splits on ``_`` to parts[0]=``mcp`` / parts[-1]=``employees`` and
    the read verb ``list`` sits in the middle, unseen, so the read is mis-classified as
    a write (LOW+not-auto → pending_approval). Split on ``__`` and take the bare tool.
    """
    bare = name.split("__")[-1] if "__" in name else name
    parts = bare.lower().split("_")
    if not parts:
        return False
    return parts[0] in _READ_SUFFIXES or parts[-1] in _READ_SUFFIXES


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
