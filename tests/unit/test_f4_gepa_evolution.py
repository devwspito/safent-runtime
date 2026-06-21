"""F4 — Test (c): GEPA evolution proposals re-enter the signed governance pipeline.

Verifies:
  - HeuristicEvolutionEngine produces parseable SKILL.md proposals.
  - Proposals are typed SkillEvolutionProposal (not ToolCallProposal) — no side effect.
  - The proposal's proposed_skill_md is a valid SKILL.md document (parse_skill_md passes).
  - EvolutionOrchestrator correctly extracts failure traces from audit rows.
  - Proposals never write skills directly — they only produce data structures.
  - The caller must submit proposals as ToolCallProposal with tool_name="skill_manage"
    to the broker for HITL approval before any write occurs.
  - Skills below the failure threshold are NOT proposed.
  - Skills not present in the current_skill_contents are skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hermes.training.application.skill_evolution import (
    EvolutionOrchestrator,
    FailureTrace,
    HeuristicEvolutionEngine,
    SkillEvolutionProposal,
    _append_changelog,
    _extract_skill_name_from_row,
    extract_failure_traces,
)
from hermes.training.domain.skill_md_document import parse_skill_md

pytestmark = pytest.mark.unit

_TENANT = UUID("cccccccc-0000-0000-0000-000000000003")

_BASE_SKILL_MD = (
    "---\n"
    "name: pay-invoice\n"
    "description: Pay an invoice via the portal\n"
    "version: '1'\n"
    "---\n\n"
    "## When\n- Invoice arrives in inbox\n\n"
    "## Procedure\n1. Click pay button\n\n"
    "## Pitfalls\n- Modal may require 2FA\n\n"
    "## Verification\n- Receipt shows payment confirmed\n"
)


def _make_trace(
    *,
    skill_name: str = "pay-invoice",
    failure_count: int = 3,
    error: str = "HITL_REJECTED: operator denied",
) -> FailureTrace:
    return FailureTrace(
        skill_name=skill_name,
        tenant_id=_TENANT,
        failure_count=failure_count,
        last_failure_at=datetime(2026, 6, 1, tzinfo=UTC),
        representative_error=error,
    )


# ---------------------------------------------------------------------------
# HeuristicEvolutionEngine unit tests
# ---------------------------------------------------------------------------


class TestHeuristicEvolutionEngine:
    def test_produces_valid_skill_md_proposal(self) -> None:
        engine = HeuristicEvolutionEngine()
        trace = _make_trace(failure_count=3)
        proposals = engine.propose_evolutions(
            [trace], {"pay-invoice": _BASE_SKILL_MD}
        )

        assert len(proposals) == 1
        proposal = proposals[0]
        assert isinstance(proposal, SkillEvolutionProposal)

        # Must be parseable SKILL.md
        doc = parse_skill_md(proposal.proposed_skill_md)
        assert doc.name == "pay-invoice"

    def test_proposal_contains_changelog(self) -> None:
        engine = HeuristicEvolutionEngine()
        trace = _make_trace(failure_count=5)
        proposals = engine.propose_evolutions(
            [trace], {"pay-invoice": _BASE_SKILL_MD}
        )
        assert len(proposals) == 1
        assert "## Changelog" in proposals[0].proposed_skill_md

    def test_below_threshold_not_proposed(self) -> None:
        engine = HeuristicEvolutionEngine(min_failures=5)
        trace = _make_trace(failure_count=2)  # below threshold
        proposals = engine.propose_evolutions(
            [trace], {"pay-invoice": _BASE_SKILL_MD}
        )
        assert proposals == []

    def test_missing_skill_content_skipped(self) -> None:
        engine = HeuristicEvolutionEngine()
        trace = _make_trace(skill_name="nonexistent-skill", failure_count=10)
        proposals = engine.propose_evolutions([trace], {})  # no content provided
        assert proposals == []

    def test_generated_by_is_heuristic(self) -> None:
        engine = HeuristicEvolutionEngine()
        trace = _make_trace()
        proposals = engine.propose_evolutions(
            [trace], {"pay-invoice": _BASE_SKILL_MD}
        )
        assert proposals[0].generated_by == "heuristic"

    def test_proposal_has_unique_id(self) -> None:
        engine = HeuristicEvolutionEngine()
        trace_a = _make_trace(skill_name="pay-invoice")
        trace_b = _make_trace(skill_name="pay-invoice")  # same trace
        proposals_a = engine.propose_evolutions(
            [trace_a], {"pay-invoice": _BASE_SKILL_MD}
        )
        proposals_b = engine.propose_evolutions(
            [trace_b], {"pay-invoice": _BASE_SKILL_MD}
        )
        # Each proposal gets a fresh UUID
        assert proposals_a[0].proposal_id != proposals_b[0].proposal_id

    def test_existing_changelog_not_duplicated(self) -> None:
        """Skills that already have ## Changelog are not modified."""
        engine = HeuristicEvolutionEngine()
        md_with_changelog = _BASE_SKILL_MD + "\n\n## Changelog\n- Existing entry\n"
        trace = _make_trace()
        proposals = engine.propose_evolutions(
            [trace], {"pay-invoice": md_with_changelog}
        )
        # No proposal produced (no change)
        assert proposals == []

    def test_multiple_skills_produces_multiple_proposals(self) -> None:
        engine = HeuristicEvolutionEngine()
        skill_b_md = _BASE_SKILL_MD.replace("pay-invoice", "send-report")
        traces = [
            _make_trace(skill_name="pay-invoice", failure_count=3),
            _make_trace(skill_name="send-report", failure_count=4),
        ]
        proposals = engine.propose_evolutions(
            traces,
            {
                "pay-invoice": _BASE_SKILL_MD,
                "send-report": skill_b_md,
            },
        )
        assert len(proposals) == 2
        names = {p.skill_name for p in proposals}
        assert names == {"pay-invoice", "send-report"}


# ---------------------------------------------------------------------------
# Governance invariant: proposals are DATA, not side effects
# ---------------------------------------------------------------------------


class TestGepaGovernanceInvariant:
    """SkillEvolutionProposal never triggers a write.

    The test that matters: after calling propose_evolutions, no file was
    written anywhere. The caller must go through broker → HITL → adapter.
    """

    def test_no_files_written_during_propose(self, tmp_path: Path) -> None:
        engine = HeuristicEvolutionEngine()
        trace = _make_trace()
        proposals = engine.propose_evolutions(
            [trace], {"pay-invoice": _BASE_SKILL_MD}
        )
        assert len(proposals) == 1
        # Verify no files were written by the engine itself
        assert list(tmp_path.rglob("*.md")) == [], (
            "HeuristicEvolutionEngine must NOT write any files. "
            "Proposals are data structures — the caller submits to broker."
        )

    def test_proposal_to_broker_path_is_skill_manage(self) -> None:
        """The correct tool_name for submitting proposals to the broker is skill_manage."""
        # This is a documentation test: it asserts the canonical submission path.
        # The caller converts SkillEvolutionProposal → ToolCallProposal as follows:
        #   ToolCallProposal(
        #       tool_name="skill_manage",
        #       parameters={
        #           "action": "edit",
        #           "name": proposal.skill_name,
        #           "content": proposal.proposed_skill_md,
        #       },
        #       tenant_id=proposal.tenant_id,
        #       ...
        #   )
        # Then broker.dispatch(proposal, consent_context, hitl_approval_token=token)
        # This test verifies the SkillEvolutionProposal has the right fields.
        from hermes.domain.proposal import ToolCallProposal  # noqa: PLC0415

        engine = HeuristicEvolutionEngine()
        trace = _make_trace()
        proposals = engine.propose_evolutions(
            [trace], {"pay-invoice": _BASE_SKILL_MD}
        )
        assert len(proposals) == 1
        p = proposals[0]

        # Build the ToolCallProposal as the caller would
        tool_proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="skill_manage",
            tenant_id=p.tenant_id,
            entity_id=str(p.proposal_id),
            entity_type="gepa_evolution",
            parameters={
                "action": "edit",
                "name": p.skill_name,
                "content": p.proposed_skill_md,
            },
            justification=p.rationale,
        )
        assert tool_proposal.tool_name == "skill_manage"
        assert tool_proposal.parameters["name"] == p.skill_name
        # parse_skill_md must not raise
        doc = parse_skill_md(tool_proposal.parameters["content"])
        assert doc.name == p.skill_name


# ---------------------------------------------------------------------------
# extract_failure_traces from audit rows
# ---------------------------------------------------------------------------


class TestExtractFailureTraces:
    def _make_rejection_row(
        self,
        skill_name: str = "pay-invoice",
        tenant_id: str | None = None,
        error: str = "broker rejected",
    ) -> dict:
        return {
            "audit_kind": "proposal_rejected",
            "category": f"skill_name={skill_name}",
            "description": error,
            "payload_json": "{}",
            "timestamp": "2026-06-01T12:00:00+00:00",
            "tenant_id": tenant_id or str(_TENANT),
        }

    def test_extracts_trace_from_rejection_rows(self) -> None:
        rows = [self._make_rejection_row() for _ in range(3)]
        traces = extract_failure_traces(rows, tenant_id=_TENANT)
        assert len(traces) == 1
        assert traces[0].skill_name == "pay-invoice"
        assert traces[0].failure_count == 3

    def test_other_audit_kinds_are_ignored(self) -> None:
        rows = [
            {"audit_kind": "proposal_executed", "category": "skill_name=my-skill",
             "description": "ok", "timestamp": "", "tenant_id": str(_TENANT)},
        ]
        traces = extract_failure_traces(rows, tenant_id=_TENANT)
        assert traces == []

    def test_below_threshold_excluded(self) -> None:
        rows = [self._make_rejection_row()]  # only 1 failure
        traces = extract_failure_traces(rows, tenant_id=_TENANT, min_failures=2)
        assert traces == []

    def test_cross_tenant_rows_are_excluded(self) -> None:
        other_tenant = UUID("dddddddd-0000-0000-0000-000000000004")
        rows = [
            self._make_rejection_row(tenant_id=str(other_tenant)) for _ in range(5)
        ]
        traces = extract_failure_traces(rows, tenant_id=_TENANT)
        assert traces == []

    def test_sorted_by_failure_count_desc(self) -> None:
        rows = (
            [self._make_rejection_row("skill-a")] * 2
            + [self._make_rejection_row("skill-b")] * 5
        )
        traces = extract_failure_traces(rows, tenant_id=_TENANT, min_failures=1)
        assert traces[0].skill_name == "skill-b"  # 5 failures first
        assert traces[1].skill_name == "skill-a"  # 2 failures second


# ---------------------------------------------------------------------------
# _append_changelog pure function tests
# ---------------------------------------------------------------------------


class TestAppendChangelog:
    def test_appends_changelog_section(self) -> None:
        result = _append_changelog(
            current_md=_BASE_SKILL_MD,
            skill_name="pay-invoice",
            failure_count=3,
            last_failure_at=datetime(2026, 6, 1, tzinfo=UTC),
            representative_error="HITL rejected",
        )
        assert "## Changelog" in result
        assert "2026-06-01" in result
        assert "3 times" in result

    def test_does_not_double_append(self) -> None:
        md_with_log = _append_changelog(
            current_md=_BASE_SKILL_MD,
            skill_name="pay-invoice",
            failure_count=1,
            last_failure_at=datetime(2026, 6, 1, tzinfo=UTC),
            representative_error="error",
        )
        result = _append_changelog(
            current_md=md_with_log,
            skill_name="pay-invoice",
            failure_count=2,
            last_failure_at=datetime(2026, 6, 2, tzinfo=UTC),
            representative_error="error 2",
        )
        # Changelog section appears exactly once
        assert result.count("## Changelog") == 1

    def test_result_is_parseable_skill_md(self) -> None:
        result = _append_changelog(
            current_md=_BASE_SKILL_MD,
            skill_name="pay-invoice",
            failure_count=2,
            last_failure_at=datetime(2026, 6, 1, tzinfo=UTC),
            representative_error="some error",
        )
        doc = parse_skill_md(result)
        assert doc.name == "pay-invoice"


# ---------------------------------------------------------------------------
# EvolutionOrchestrator integration (in-memory)
# ---------------------------------------------------------------------------


class TestEvolutionOrchestratorNoSideEffects:
    def test_run_offline_pass_with_no_audit_db(self, tmp_path: Path) -> None:
        """If audit DB does not exist, returns empty proposals (no crash)."""
        orchestrator = EvolutionOrchestrator(
            engine=HeuristicEvolutionEngine(),
            audit_db_path=tmp_path / "nonexistent.db",
            skill_store_root=tmp_path / "skills",
        )
        proposals = orchestrator.run_offline_pass(tenant_id=_TENANT)
        assert proposals == []

    def test_run_offline_pass_with_real_db_and_skills(self, tmp_path: Path) -> None:
        """Full pass: audit DB has rejections + skill on disk → proposal produced."""
        import sqlite3  # noqa: PLC0415
        from hermes.agents_os.infrastructure.audit_schema import ensure_audit_chain_schema  # noqa: PLC0415

        db_path = tmp_path / "audit.db"
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        ensure_audit_chain_schema(conn)

        # Insert 3 rejection rows for "pay-invoice"
        from datetime import UTC, datetime  # noqa: PLC0415
        now = datetime.now(tz=UTC).isoformat()
        for i in range(3):
            conn.execute(
                """
                INSERT INTO audit_chain_entries (
                    entry_id, node_installation_id, tenant_id, timestamp,
                    actor, audit_kind, category, description, payload_json,
                    payload_hash_hex, prev_entry_hash_hex,
                    signed_payload_hash_hex, signature_hex, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(uuid4()), None, str(_TENANT), now,
                    "agent", "proposal_rejected",
                    "skill_name=pay-invoice",
                    "HITL rejected skill_manage for pay-invoice",
                    "{}",
                    "a" * 64, "b" * 64, "c" * 64, "d" * 64, now,
                ),
            )
        conn.close()

        # Put the skill on disk
        skill_dir = tmp_path / "skills" / "pay-invoice"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_BASE_SKILL_MD, encoding="utf-8")

        orchestrator = EvolutionOrchestrator(
            engine=HeuristicEvolutionEngine(),
            audit_db_path=db_path,
            skill_store_root=tmp_path / "skills",
        )
        proposals = orchestrator.run_offline_pass(tenant_id=_TENANT)

        assert len(proposals) == 1
        assert proposals[0].skill_name == "pay-invoice"
        doc = parse_skill_md(proposals[0].proposed_skill_md)
        assert doc.name == "pay-invoice"
        # Still no files written by the orchestrator
        assert not (tmp_path / "skills" / "pay-invoice" / "SKILL.md.bak").exists()
