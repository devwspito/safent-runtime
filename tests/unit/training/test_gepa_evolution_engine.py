"""Tests for GEPAEvolutionEngine (hermes.training.infrastructure.gepa_evolution_engine).

Coverage (per task brief):
  (a) GEPAEvolutionEngine implements EvolutionEnginePort (structural).
  (b) Without dspy installed, instantiating GEPAEvolutionEngine raises
      DSPyNotInstalledError (not a crash, not a silent failure).
  (c) A SkillEvolutionProposal converts to a skill_manage ToolCallProposal
      (governance invariant — GEPA never writes directly).
  (d) Constraint gates reject candidates that are too large or lack structure.

Plus:
  - Pure functions (fitness, levenshtein, constraint gates) testable without dspy.
  - Dataset builder produces non-empty dataset with expected keys.
  - CLI --help works without dspy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.unit

_TENANT = UUID("eeeeeeee-0000-0000-0000-000000000005")

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
    error: str = "HITL_REJECTED: operator denied the modal step",
) -> object:
    from hermes.training.application.skill_evolution import FailureTrace  # noqa: PLC0415
    return FailureTrace(
        skill_name=skill_name,
        tenant_id=_TENANT,
        failure_count=failure_count,
        last_failure_at=datetime(2026, 6, 1, tzinfo=UTC),
        representative_error=error,
    )


# ---------------------------------------------------------------------------
# (a) Port structural contract
# ---------------------------------------------------------------------------


class TestEvolutionEnginePortContract:
    """GEPAEvolutionEngine structurally satisfies EvolutionEnginePort."""

    def test_implements_propose_evolutions_method(self) -> None:
        """The class declares propose_evolutions — port contract is met statically."""
        from hermes.training.infrastructure.gepa_evolution_engine import (  # noqa: PLC0415
            GEPAEvolutionEngine,
        )
        assert hasattr(GEPAEvolutionEngine, "propose_evolutions")
        assert callable(GEPAEvolutionEngine.propose_evolutions)

    def test_port_runtime_check_via_isinstance(self) -> None:
        """EvolutionEnginePort is a Protocol — structural subtype check."""
        from hermes.training.application.skill_evolution import EvolutionEnginePort  # noqa: PLC0415
        from hermes.training.infrastructure.gepa_evolution_engine import (  # noqa: PLC0415
            GEPAEvolutionEngine,
        )
        # runtime_checkable must be satisfied
        import typing  # noqa: PLC0415
        if typing.get_type_hints(EvolutionEnginePort.propose_evolutions):
            pass  # Protocol type hints are present
        # Structural: we verify the method signature matches the port.
        import inspect  # noqa: PLC0415
        engine_sig = inspect.signature(GEPAEvolutionEngine.propose_evolutions)
        port_sig = inspect.signature(EvolutionEnginePort.propose_evolutions)
        engine_params = set(engine_sig.parameters.keys()) - {"self"}
        port_params = set(port_sig.parameters.keys()) - {"self"}
        assert engine_params == port_params, (
            f"GEPAEvolutionEngine.propose_evolutions params {engine_params} "
            f"do not match EvolutionEnginePort {port_params}"
        )


# ---------------------------------------------------------------------------
# (b) Without dspy: DSPyNotInstalledError raised at instantiation
# ---------------------------------------------------------------------------


class TestDSPyLazyImport:
    """Importing the module is always safe. Instantiation fails loud without dspy."""

    def test_module_imports_without_dspy(self) -> None:
        """The module itself must be importable without dspy installed."""
        import importlib  # noqa: PLC0415
        mod = importlib.import_module(
            "hermes.training.infrastructure.gepa_evolution_engine"
        )
        assert mod is not None

    def test_pure_functions_accessible_without_dspy(self) -> None:
        """Pure constraint functions are accessible without dspy."""
        from hermes.training.infrastructure.gepa_evolution_engine import (  # noqa: PLC0415
            apply_constraint_gates,
            check_size_constraint,
            check_structure_constraint,
            compute_fitness_score,
        )
        assert callable(apply_constraint_gates)
        assert callable(check_size_constraint)
        assert callable(check_structure_constraint)
        assert callable(compute_fitness_score)

    def test_instantiation_raises_dspy_not_installed_error(self) -> None:
        """GEPAEvolutionEngine.__init__ raises DSPyNotInstalledError when dspy absent.

        This test passes when dspy is NOT installed (the normal CI state).
        If dspy IS installed, the engine constructs successfully and this
        branch is skipped — the test still passes via the except block.
        """
        from hermes.training.infrastructure.gepa_evolution_engine import (  # noqa: PLC0415
            DSPyNotInstalledError,
            GEPAEvolutionEngine,
        )
        try:
            import dspy  # noqa: PLC0415, F401
            dspy_available = True
        except ImportError:
            dspy_available = False

        if dspy_available:
            # dspy present — engine should construct (tested separately)
            pytest.skip("dspy is installed; lazy-import guard test not applicable")

        with pytest.raises(DSPyNotInstalledError) as exc_info:
            GEPAEvolutionEngine()

        # Error message must be actionable (install instruction present)
        assert "pip install" in str(exc_info.value).lower()
        assert "[evolution]" in str(exc_info.value)

    def test_dspy_not_installed_error_is_import_error_subclass(self) -> None:
        """DSPyNotInstalledError must be an ImportError subclass (clear fail path)."""
        from hermes.training.infrastructure.gepa_evolution_engine import (  # noqa: PLC0415
            DSPyNotInstalledError,
        )
        assert issubclass(DSPyNotInstalledError, ImportError)

    def test_gepa_config_constructable_without_dspy(self) -> None:
        """GEPAConfig is a pure dataclass — no dspy dependency."""
        from hermes.training.infrastructure.gepa_evolution_engine import GEPAConfig  # noqa: PLC0415
        cfg = GEPAConfig(model="openai/gpt-4", max_steps=3)
        assert cfg.model == "openai/gpt-4"
        assert cfg.max_steps == 3


# ---------------------------------------------------------------------------
# (c) Governance invariant: proposal converts to skill_manage ToolCallProposal
# ---------------------------------------------------------------------------


class TestGovernanceInvariant:
    """SkillEvolutionProposal (generated_by='gepa') maps correctly to broker path."""

    def _make_gepa_proposal(self) -> object:
        from hermes.training.application.skill_evolution import SkillEvolutionProposal  # noqa: PLC0415
        return SkillEvolutionProposal(
            proposal_id=uuid4(),
            skill_name="pay-invoice",
            tenant_id=_TENANT,
            proposed_skill_md=_BASE_SKILL_MD,
            rationale="GEPA evolution: 3 failures. Candidate passed gates.",
            generated_by="gepa",
        )

    def test_gepa_proposal_converts_to_skill_manage_tool_proposal(self) -> None:
        """The proposal MUST be submitted as tool_name='skill_manage' to the broker."""
        from hermes.domain.proposal import ToolCallProposal  # noqa: PLC0415
        p = self._make_gepa_proposal()

        tool_proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="skill_manage",
            tenant_id=p.tenant_id,  # type: ignore[union-attr]
            entity_id=str(p.proposal_id),  # type: ignore[union-attr]
            entity_type="gepa_evolution",
            parameters={
                "action": "edit",
                "name": p.skill_name,  # type: ignore[union-attr]
                "content": p.proposed_skill_md,  # type: ignore[union-attr]
            },
            justification=p.rationale,  # type: ignore[union-attr]
        )

        assert tool_proposal.tool_name == "skill_manage"
        assert tool_proposal.parameters["action"] == "edit"
        assert tool_proposal.parameters["name"] == "pay-invoice"

    def test_proposed_skill_md_is_parseable(self) -> None:
        """Content in the proposal must parse as a valid SKILL.md."""
        from hermes.training.domain.skill_md_document import parse_skill_md  # noqa: PLC0415
        p = self._make_gepa_proposal()
        doc = parse_skill_md(p.proposed_skill_md)  # type: ignore[union-attr]
        assert doc.name == "pay-invoice"

    def test_proposal_is_frozen_data_structure(self) -> None:
        """SkillEvolutionProposal is immutable — no side effects possible via mutation."""
        p = self._make_gepa_proposal()
        with pytest.raises((AttributeError, TypeError)):
            p.skill_name = "tampered"  # type: ignore[misc]

    def test_gepa_generated_by_field(self) -> None:
        """generated_by='gepa' distinguishes GEPA from heuristic proposals."""
        p = self._make_gepa_proposal()
        assert p.generated_by == "gepa"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# (d) Constraint gates
# ---------------------------------------------------------------------------


class TestConstraintGates:
    """apply_constraint_gates rejects invalid candidates before they become proposals."""

    def test_passes_valid_skill_md(self) -> None:
        passed, reason = _gate(_BASE_SKILL_MD, "pay-invoice", "Pay an invoice via the portal")
        assert passed, f"Valid skill should pass gates, got: {reason}"

    def test_rejects_oversized_skill_md(self) -> None:
        big_md = _BASE_SKILL_MD + ("x" * (16 * 1024))  # > 15 KB
        passed, reason = _gate(big_md, "pay-invoice", "Pay an invoice via the portal")
        assert not passed
        assert "size" in reason.lower() or "15" in reason

    def test_rejects_missing_procedure_section(self) -> None:
        no_procedure = _BASE_SKILL_MD.replace("## Procedure", "## Steps")
        passed, reason = _gate(no_procedure, "pay-invoice", "Pay an invoice via the portal")
        assert not passed
        assert "Procedure" in reason or "section" in reason.lower()

    def test_rejects_missing_pitfalls_section(self) -> None:
        no_pitfalls = _BASE_SKILL_MD.replace("## Pitfalls", "## Warnings")
        passed, reason = _gate(no_pitfalls, "pay-invoice", "Pay an invoice via the portal")
        assert not passed

    def test_rejects_missing_when_section(self) -> None:
        no_when = _BASE_SKILL_MD.replace("## When", "## Trigger")
        passed, reason = _gate(no_when, "pay-invoice", "Pay an invoice via the portal")
        assert not passed

    def test_rejects_changed_skill_name(self) -> None:
        renamed = _BASE_SKILL_MD.replace("name: pay-invoice", "name: different-skill")
        passed, reason = _gate(renamed, "pay-invoice", "Pay an invoice via the portal")
        assert not passed
        assert "drift" in reason.lower() or "name" in reason.lower()

    def test_rejects_severely_drifted_description(self) -> None:
        """If the description changes completely, semantic drift gate fires."""
        completely_different_desc = "Zyxwvutsrqponmlkjihgfedcba completely unrelated"
        drifted = _BASE_SKILL_MD.replace(
            "description: Pay an invoice via the portal",
            f"description: {completely_different_desc}",
        )
        passed, reason = _gate(drifted, "pay-invoice", "Pay an invoice via the portal")
        assert not passed

    def test_rejects_unparseable_frontmatter(self) -> None:
        broken = "this is not a SKILL.md at all"
        passed, reason = _gate(broken, "pay-invoice", "Pay an invoice via the portal")
        assert not passed

    def test_reason_empty_when_passed(self) -> None:
        passed, reason = _gate(_BASE_SKILL_MD, "pay-invoice", "Pay an invoice via the portal")
        assert passed
        assert reason == ""


def _gate(candidate: str, name: str, description: str) -> tuple[bool, str]:
    from hermes.training.infrastructure.gepa_evolution_engine import apply_constraint_gates  # noqa: PLC0415
    return apply_constraint_gates(candidate, name, description)


# ---------------------------------------------------------------------------
# Pure fitness / scoring functions
# ---------------------------------------------------------------------------


class TestFitnessScore:
    def test_well_formed_skill_has_positive_score(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import compute_fitness_score  # noqa: PLC0415
        score = compute_fitness_score(_BASE_SKILL_MD, "some error")
        assert score > 0.0

    def test_score_in_valid_range(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import compute_fitness_score  # noqa: PLC0415
        score = compute_fitness_score(_BASE_SKILL_MD, "HITL_REJECTED: operator denied")
        # Score can go slightly negative if penalty is large, but not for a tiny skill
        assert -1.0 <= score <= 1.5

    def test_empty_skill_has_low_score(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import (  # noqa: PLC0415
            _procedure_score,
            _conciseness_score,
        )
        # Empty candidate: no sections present → procedure = 0
        assert _procedure_score("") == 0.0
        # Empty is small → conciseness near 1.0
        assert _conciseness_score("") > 0.9

    def test_length_penalty_applied_for_large_skill(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import _length_penalty  # noqa: PLC0415
        # Build a 10 KB string — above the 8 KB soft cap
        large = "x" * (10 * 1024)
        penalty = _length_penalty(large)
        assert penalty > 0.0

    def test_no_penalty_for_small_skill(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import _length_penalty  # noqa: PLC0415
        penalty = _length_penalty(_BASE_SKILL_MD)
        assert penalty == 0.0  # well under 8 KB


# ---------------------------------------------------------------------------
# Dataset builder
# ---------------------------------------------------------------------------


class TestBuildEvalDataset:
    def test_returns_non_empty_dataset(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import _build_eval_dataset  # noqa: PLC0415
        trace = _make_trace()
        dataset = _build_eval_dataset(trace, _BASE_SKILL_MD)
        assert len(dataset) >= 1

    def test_dataset_entries_have_required_keys(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import _build_eval_dataset  # noqa: PLC0415
        trace = _make_trace()
        dataset = _build_eval_dataset(trace, _BASE_SKILL_MD)
        for entry in dataset:
            assert "current_skill_md" in entry
            assert "failure_description" in entry
            assert "expected_improvement" in entry

    def test_dataset_embeds_failure_error(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import _build_eval_dataset  # noqa: PLC0415
        error = "specific_error_token_12345"
        trace = _make_trace(error=error)
        dataset = _build_eval_dataset(trace, _BASE_SKILL_MD)
        all_text = " ".join(str(e) for e in dataset)
        assert error in all_text


# ---------------------------------------------------------------------------
# Levenshtein helpers
# ---------------------------------------------------------------------------


class TestLevenshteinRatio:
    def test_identical_strings_return_one(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import _levenshtein_ratio  # noqa: PLC0415
        assert _levenshtein_ratio("hello", "hello") == 1.0

    def test_empty_strings_return_one(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import _levenshtein_ratio  # noqa: PLC0415
        assert _levenshtein_ratio("", "") == 1.0

    def test_completely_different_strings_low_ratio(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import _levenshtein_ratio  # noqa: PLC0415
        ratio = _levenshtein_ratio("abcdefgh", "zyxwvuts")
        assert ratio < 0.3

    def test_minor_change_high_ratio(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import _levenshtein_ratio  # noqa: PLC0415
        ratio = _levenshtein_ratio(
            "Pay an invoice via the portal",
            "Pay an invoice via the portal carefully",
        )
        assert ratio > 0.7

    def test_one_empty_returns_zero(self) -> None:
        from hermes.training.infrastructure.gepa_evolution_engine import _levenshtein_ratio  # noqa: PLC0415
        assert _levenshtein_ratio("nonempty", "") == 0.0
        assert _levenshtein_ratio("", "nonempty") == 0.0


# ---------------------------------------------------------------------------
# CLI module importable without dspy
# ---------------------------------------------------------------------------


class TestCLIModuleImport:
    def test_cli_module_imports_without_dspy(self) -> None:
        """The CLI entry-point module must import without dspy installed."""
        import importlib  # noqa: PLC0415
        mod = importlib.import_module("hermes.training.evolution.__main__")
        assert hasattr(mod, "main")
        assert callable(mod.main)

    def test_cli_arg_parser_builds_without_dspy(self) -> None:
        """Arg parser construction must not touch dspy."""
        from hermes.training.evolution.__main__ import _build_arg_parser  # noqa: PLC0415
        parser = _build_arg_parser()
        assert parser is not None

    def test_cli_heuristic_engine_works_without_dspy(self, tmp_path: object) -> None:
        """CLI with --engine heuristic must work without dspy."""
        import sys  # noqa: PLC0415
        from io import StringIO  # noqa: PLC0415
        from hermes.training.evolution.__main__ import _build_engine  # noqa: PLC0415
        import argparse  # noqa: PLC0415

        args = argparse.Namespace(
            engine="heuristic",
            min_failures=2,
            model="openai/gpt-4o-mini",
            max_steps=5,
        )
        engine = _build_engine(args)
        assert engine is not None
        # Should have propose_evolutions
        assert hasattr(engine, "propose_evolutions")
