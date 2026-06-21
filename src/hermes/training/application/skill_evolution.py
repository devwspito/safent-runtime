"""skill_evolution — GEPA offline evolution scaffold (F4).

Reads signed audit traces from audit_chain_entries, identifies candidate
skills (those that failed or required repeated proposals), and produces a
proposed improved SKILL.md that RE-ENTERS the signed governance pipeline:
    skill_manage → broker → HITL → SkillStoreAdapter (sign + persist)

NEVER writes skills directly. Every proposed improvement goes through:
    1. EvolutionEngine.propose_evolutions() → list[SkillEvolutionProposal]
    2. Caller submits each proposal as a ToolCallProposal with tool_name="skill_manage"
       to the capability broker (same path as Nous autonomous skill_manage).
    3. Broker gates: HITL token required (HIGH risk, auto_executable=False).
    4. After HITL approval: SkillStoreAdapter signs and persists.

Architecture:
    - EvolutionEnginePort (port, domain layer): abstract interface.
    - HeuristicEvolutionEngine (stub impl): simple failure-count heuristic.
      Identifies skills used in PROPOSAL_REJECTED entries and emits an
      improved SKILL.md with a changelog section appended.
    - GEPAEvolutionEngine (future): plugs into this port. Will use DSPy +
      NousResearch hermes-agent-self-evolution repo. NOT implemented here.
      See _GEPA_INTEGRATION_NOTES at the bottom of this module.

Dependency graph (one-way):
    domain.skill_md_document ← training.application.skill_evolution
    domain.audit_hash_chain  ← training.application.skill_evolution
    (No HTTP, no DB driver, no framework in domain path)

Capa: application (orchestrates domain types). No I/O in pure functions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, Sequence
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureTrace:
    """A single failed or repeated skill proposal from the audit log.

    Extracted from audit_chain_entries (AuditKind.PROPOSAL_REJECTED) when
    category or description references a known skill.
    """

    skill_name: str
    tenant_id: UUID
    failure_count: int
    last_failure_at: datetime
    representative_error: str


@dataclass(frozen=True, slots=True)
class SkillEvolutionProposal:
    """Proposed improvement to a SKILL.md.

    The proposed_skill_md content is a valid SKILL.md document (passes
    parse_skill_md). The caller submits it as a ToolCallProposal with
    tool_name="skill_manage", action="edit", name=skill_name,
    content=proposed_skill_md to the capability broker.

    INVARIANT: This object never triggers a write on its own.
               Only broker.dispatch() after HITL approval produces an effect.
    """

    proposal_id: UUID
    skill_name: str
    tenant_id: UUID
    proposed_skill_md: str
    rationale: str
    generated_by: str  # "heuristic" or "gepa" (future)


# ---------------------------------------------------------------------------
# Port (domain / application layer)
# ---------------------------------------------------------------------------


class EvolutionEnginePort(Protocol):
    """Port for the skill evolution engine.

    Implementations:
      - HeuristicEvolutionEngine: simple failure-count heuristic (this file).
      - GEPAEvolutionEngine: DSPy + NousResearch self-evolution (future).

    Callers:
      - EvolutionOrchestrator.run_offline_pass() — not in the hot loop.
        Called by an offline cron trigger or a manual operator command.
    """

    def propose_evolutions(
        self,
        traces: Sequence[FailureTrace],
        current_skill_contents: dict[str, str],
    ) -> list[SkillEvolutionProposal]:
        """Return evolution proposals for the given failure traces.

        Args:
            traces:                Failure traces from audit log.
            current_skill_contents: Map of skill_name → current SKILL.md text.
                                   Skills not in this map are skipped.

        Returns:
            List of proposals. May be empty if no evolution is warranted.
            Each proposal has a valid proposed_skill_md (parseable SKILL.md).
        """
        ...


# ---------------------------------------------------------------------------
# Heuristic stub implementation
# ---------------------------------------------------------------------------

_CHANGELOG_SECTION = "## Changelog"
_MIN_FAILURES_FOR_EVOLUTION = 2


class HeuristicEvolutionEngine:
    """Simple failure-count heuristic evolution engine.

    Produces improvement proposals for skills that failed >= threshold times.
    The improvement is minimal: appends a Changelog section documenting the
    failures and a generic "Review and refine procedure" note.

    This is a STUB — the proposals require human HITL review before effect.
    A principal engineer or operator reviews each proposed SKILL.md before
    approving via the standard HITL flow.

    The GEPA real engine (DSPy-based) plugs in at the EvolutionEnginePort
    interface. See _GEPA_INTEGRATION_NOTES below.
    """

    def __init__(self, *, min_failures: int = _MIN_FAILURES_FOR_EVOLUTION) -> None:
        self._min_failures = min_failures

    def propose_evolutions(
        self,
        traces: Sequence[FailureTrace],
        current_skill_contents: dict[str, str],
    ) -> list[SkillEvolutionProposal]:
        proposals = []
        for trace in traces:
            if trace.failure_count < self._min_failures:
                continue
            current_md = current_skill_contents.get(trace.skill_name)
            if not current_md:
                logger.debug(
                    "hermes.evolution.skip_missing_skill name=%s", trace.skill_name
                )
                continue

            proposal = self._build_proposal(trace, current_md)
            if proposal is not None:
                proposals.append(proposal)
                logger.info(
                    "hermes.evolution.proposal_generated skill=%s failures=%d engine=heuristic",
                    trace.skill_name,
                    trace.failure_count,
                )
        return proposals

    def _build_proposal(
        self, trace: FailureTrace, current_md: str
    ) -> SkillEvolutionProposal | None:
        """Append a Changelog section to the current SKILL.md."""
        proposed_md = _append_changelog(
            current_md=current_md,
            skill_name=trace.skill_name,
            failure_count=trace.failure_count,
            last_failure_at=trace.last_failure_at,
            representative_error=trace.representative_error,
        )
        if proposed_md == current_md:
            return None  # No change produced

        try:
            from hermes.training.domain.skill_md_document import parse_skill_md  # noqa: PLC0415
            parse_skill_md(proposed_md)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.evolution.invalid_proposed_skill_md skill=%s: %s",
                trace.skill_name,
                exc,
            )
            return None

        return SkillEvolutionProposal(
            proposal_id=uuid4(),
            skill_name=trace.skill_name,
            tenant_id=trace.tenant_id,
            proposed_skill_md=proposed_md,
            rationale=(
                f"Skill failed {trace.failure_count} times. "
                f"Last error: {trace.representative_error[:120]}. "
                "Heuristic evolution: Changelog section appended for human review."
            ),
            generated_by="heuristic",
        )


# ---------------------------------------------------------------------------
# Audit log reader
# ---------------------------------------------------------------------------


def extract_failure_traces(
    audit_rows: Sequence[dict],
    *,
    tenant_id: UUID,
    min_failures: int = _MIN_FAILURES_FOR_EVOLUTION,
) -> list[FailureTrace]:
    """Extract FailureTrace objects from audit_chain_entries rows.

    Args:
        audit_rows:  Sequence of dicts from audit_chain_entries SELECT.
                     Expected keys: audit_kind, category, description,
                     payload_json, timestamp, tenant_id.
        tenant_id:   Scope filter — only rows for this tenant are processed.
        min_failures: Minimum failure count to include a trace.

    Returns:
        List of FailureTrace, one per skill that met the threshold.
        Sorted by failure_count descending (worst offenders first).
    """
    from hermes.agents_os.application.audit_hash_chain import AuditKind  # noqa: PLC0415

    failure_counts: dict[str, int] = {}
    last_failures: dict[str, datetime] = {}
    last_errors: dict[str, str] = {}

    for row in audit_rows:
        if row.get("audit_kind") != AuditKind.PROPOSAL_REJECTED:
            continue
        row_tenant = row.get("tenant_id")
        if row_tenant and row_tenant != str(tenant_id):
            continue

        skill_name = _extract_skill_name_from_row(row)
        if not skill_name:
            continue

        ts = _parse_timestamp(row.get("timestamp", ""))
        failure_counts[skill_name] = failure_counts.get(skill_name, 0) + 1
        if skill_name not in last_failures or ts > last_failures[skill_name]:
            last_failures[skill_name] = ts
            last_errors[skill_name] = row.get("description", "")[:256]

    traces = [
        FailureTrace(
            skill_name=name,
            tenant_id=tenant_id,
            failure_count=count,
            last_failure_at=last_failures[name],
            representative_error=last_errors.get(name, ""),
        )
        for name, count in failure_counts.items()
        if count >= min_failures
    ]
    return sorted(traces, key=lambda t: t.failure_count, reverse=True)


# ---------------------------------------------------------------------------
# EvolutionOrchestrator — wires the engine to the governance pipeline
# ---------------------------------------------------------------------------


class EvolutionOrchestrator:
    """Offline orchestrator that runs one evolution pass.

    Reads audit traces → engine proposes improvements → caller submits
    each proposal as a ToolCallProposal to the capability broker.

    Usage (offline cron or operator command):
        orchestrator = EvolutionOrchestrator(
            engine=HeuristicEvolutionEngine(),
            audit_db_path=Path("/var/lib/hermes/shell-state.db"),
            skill_store_root=Path("/var/lib/hermes/skills"),
        )
        proposals = orchestrator.run_offline_pass(tenant_id=tenant_id)
        # Then, for each proposal, build a ToolCallProposal and submit to broker:
        #   broker.dispatch(ToolCallProposal(
        #       tool_name="skill_manage",
        #       parameters={"action": "edit", "name": p.skill_name, "content": p.proposed_skill_md},
        #       tenant_id=p.tenant_id, ...
        #   ), consent_context, hitl_approval_token=token)
    """

    def __init__(
        self,
        *,
        engine: EvolutionEnginePort,
        audit_db_path: "Path",
        skill_store_root: "Path",
    ) -> None:
        self._engine = engine
        self._audit_db_path = audit_db_path
        self._skill_store_root = skill_store_root

    def run_offline_pass(self, *, tenant_id: UUID) -> list[SkillEvolutionProposal]:
        """Run one offline evolution pass for the given tenant.

        Returns proposals. The caller is responsible for submitting them to
        the capability broker for HITL approval. This method has NO side effects
        (it does not write any skill).
        """
        audit_rows = self._load_rejection_rows(tenant_id)
        traces = extract_failure_traces(audit_rows, tenant_id=tenant_id)
        if not traces:
            logger.info(
                "hermes.evolution.pass_complete tenant=%s proposals=0 (no failure traces)",
                str(tenant_id)[:8],
            )
            return []

        current_contents = self._load_current_skill_contents(
            {t.skill_name for t in traces}
        )
        proposals = self._engine.propose_evolutions(traces, current_contents)
        logger.info(
            "hermes.evolution.pass_complete tenant=%s traces=%d proposals=%d",
            str(tenant_id)[:8],
            len(traces),
            len(proposals),
        )
        return proposals

    def _load_rejection_rows(self, tenant_id: UUID) -> list[dict]:
        """Load PROPOSAL_REJECTED rows from audit_chain_entries."""
        import sqlite3  # noqa: PLC0415

        if not self._audit_db_path.exists():
            return []

        try:
            conn = sqlite3.connect(str(self._audit_db_path), isolation_level=None)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT audit_kind, category, description, payload_json,
                       timestamp, tenant_id
                FROM audit_chain_entries
                WHERE audit_kind = 'proposal_rejected'
                ORDER BY seq
                """
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.evolution.audit_load_failed: %s", exc)
            return []

    def _load_current_skill_contents(
        self, skill_names: set[str]
    ) -> dict[str, str]:
        """Read current SKILL.md content for the given skill names."""
        contents: dict[str, str] = {}
        for name in skill_names:
            skill_file = self._skill_store_root / name / "SKILL.md"
            if skill_file.exists():
                try:
                    contents[name] = skill_file.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.warning(
                        "hermes.evolution.skill_read_failed name=%s: %s", name, exc
                    )
        return contents


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_SKILL_NAME_RE = re.compile(r"\bskill[_\s]+(?:name[=:\s]+)?([a-z][a-z0-9_-]{0,63})", re.IGNORECASE)


def _extract_skill_name_from_row(row: dict) -> str | None:
    """Extract skill name from an audit row (description or category)."""
    for field_name in ("category", "description"):
        text = row.get(field_name, "") or ""
        m = _SKILL_NAME_RE.search(text)
        if m:
            return m.group(1)
    return None


def _parse_timestamp(ts_str: str) -> datetime:
    """Parse ISO-8601 UTC timestamp from audit row."""
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=UTC)


def _append_changelog(
    *,
    current_md: str,
    skill_name: str,
    failure_count: int,
    last_failure_at: datetime,
    representative_error: str,
) -> str:
    """Append a Changelog section to current_md if not already present."""
    if _CHANGELOG_SECTION in current_md:
        return current_md  # Already has a changelog — don't stack

    entry = (
        f"\n\n{_CHANGELOG_SECTION}\n\n"
        f"- **{last_failure_at.strftime('%Y-%m-%d')} — Heuristic evolution proposal**\n"
        f"  Skill failed {failure_count} times. "
        f"Last rejection: `{representative_error[:100]}`.\n"
        f"  Action: Review and refine the Procedure section to handle this error case.\n"
    )
    return current_md.rstrip() + entry


# ---------------------------------------------------------------------------
# _GEPA_INTEGRATION_NOTES
# ---------------------------------------------------------------------------
# To plug in the real GEPA engine (DSPy + NousResearch hermes-agent-self-evolution):
#
# 1. Install prerequisites (NOT done here — requires deps + model):
#    pip install dspy-ai
#    git clone https://github.com/NousResearch/hermes-agent-self-evolution
#
# 2. Create GEPAEvolutionEngine implementing EvolutionEnginePort:
#
#    class GEPAEvolutionEngine:
#        def __init__(self, *, dspy_model: str, ...): ...
#
#        def propose_evolutions(
#            self,
#            traces: Sequence[FailureTrace],
#            current_skill_contents: dict[str, str],
#        ) -> list[SkillEvolutionProposal]:
#            # Load DSPy optimizer, run OPRO/MIPRO over trajectories,
#            # emit improved SKILL.md per candidate.
#            ...
#
# 3. Wire GEPAEvolutionEngine as the engine in EvolutionOrchestrator:
#    orchestrator = EvolutionOrchestrator(
#        engine=GEPAEvolutionEngine(dspy_model="..."),
#        audit_db_path=...,
#        skill_store_root=...,
#    )
#
# 4. The rest of the governance pipeline is UNCHANGED:
#    - Proposals still go through broker.dispatch() with tool_name="skill_manage".
#    - HITL token still required (skill_manage is HIGH risk).
#    - SkillStoreAdapter still signs with v2 HMAC before persisting.
#    - Audit trail (AuditKind.PROPOSAL_EXECUTED) still recorded.
#
# INVARIANT (non-negotiable): GEPA NEVER writes skills without HITL.
# The EvolutionEnginePort only PROPOSES — the broker + HITL is the gate.
# ---------------------------------------------------------------------------
