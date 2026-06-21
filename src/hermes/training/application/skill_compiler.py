"""SkillCompiler — compila TrainingSession en SkillPackage DRAFT (T099, FR-013/014/017).

Pasos:
1. Verifica que ninguna DecisionRule tenga requires_review=True (FR-017).
2. Compone SkillPackage con ReplayScript + VoiceNarrative + DecisionRules.
3. Calcula content_hash sobre el contenido ejecutable real (FR-015 addendum).
4. Devuelve SkillPackage en estado DRAFT (sin firma HMAC todavía).

NFR-004: p95 ≤ 20 s para sesiones de hasta 50 steps.
La compilación es síncrona y ligera; la latencia viene del caller que ya
habrá ejecutado la inferencia LLM.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from hermes.training.domain.decision_rule import DecisionRule
from hermes.training.domain.skill_package import SkillPackage
from hermes.training.domain.skill_state import SkillState
from hermes.training.domain.training_session import TrainingSession
from hermes.training.domain.voice_narrative import VoiceNarrative

if TYPE_CHECKING:
    from hermes.training.domain.skill_md_document import SkillMdDocument

logger = logging.getLogger(__name__)


class SkillCompilationError(RuntimeError):
    """Compilación bloqueada por reglas pendientes de revisión (FR-017)."""


class SkillCompiler:
    """Compila una TrainingSession cerrada en una SkillPackage DRAFT."""

    def __init__(self, *, runtime_version: str | None = None) -> None:
        self._runtime_version = runtime_version or _get_runtime_version()

    def compile(
        self,
        *,
        session: TrainingSession,
        replay_script_id: UUID,
        narrative: VoiceNarrative,
        decision_rules: list[DecisionRule],
    ) -> SkillPackage:
        """Compone SkillPackage DRAFT con content_hash del contenido ejecutable.

        Falla con SkillCompilationError si alguna regla tiene requires_review=True.
        El content_hash cubre los patrones de todas las DecisionRules y la
        narrative transcript, vinculando la firma al contenido real de los
        artefactos (FR-015 addendum).
        """
        self._assert_no_pending_review(decision_rules)

        rule_ids = tuple(r.rule_id for r in decision_rules)
        content_hash = self.compute_content_hash(
            decision_rules=decision_rules,
            narrative=narrative,
            replay_script_id=replay_script_id,
        )

        pkg = SkillPackage(
            package_id=uuid4(),
            skill_id=uuid4(),
            skill_version=1,
            tenant_id=session.tenant_id,
            site_id=session.site_id,
            flow_id=session.site_id,
            replay_script_id=replay_script_id,
            voice_narrative_id=narrative.narrative_id,
            decision_rule_ids=rule_ids,
            state=SkillState.DRAFT,
            signature_hex="",
            signing_key_id="",
            runtime_version=self._runtime_version,
            compiled_by_operator_id=session.human_operator_id,
            content_hash=content_hash,
        )

        logger.info(
            "skill_package_compiled",
            extra={
                "tenant_id": str(session.tenant_id),
                "training_session_id": str(session.training_session_id),
                "package_id": str(pkg.package_id),
                "decision_rules": len(rule_ids),
                "narrative_completeness": narrative.completeness,
                "content_hash_prefix": content_hash[:12],
            },
        )
        return pkg

    @staticmethod
    def compute_content_hash(
        *,
        decision_rules: list[DecisionRule],
        narrative: VoiceNarrative,
        replay_script_id: UUID,
    ) -> str:
        """SHA-256 hex over the executable content of the skill artefacts.

        Covers: replay_script_id, sorted decision rule patterns+actions+markers,
        and sorted narrative fragment transcripts. Any mutation to these fields
        produces a different hash and invalidates the signature even when UUIDs
        remain the same (FR-015 addendum — mirror of PlatformModelSigner approach).

        Returns a 64-char hex string.
        """
        fragment_transcripts = sorted(
            f.transcript for f in narrative.fragments if f.transcript
        )
        rule_contents = sorted(
            json.dumps(
                {
                    "rule_id": str(r.rule_id),
                    "action": r.action,
                    "pattern": r.pattern,
                    "categorical_markers": sorted(r.categorical_markers),
                    "confidence": r.confidence,
                    "risk_level": str(r.risk_level),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            for r in decision_rules
        )
        content = {
            "replay_script_id": str(replay_script_id),
            "narrative_fragments": fragment_transcripts,
            "decision_rules": rule_contents,
        }
        canonical = json.dumps(
            content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _assert_no_pending_review(self, rules: list[DecisionRule]) -> None:
        """FR-017: bloquea firma si alguna regla requiere revisión."""
        blocked = [r for r in rules if r.requires_review]
        if blocked:
            ids = [str(r.rule_id) for r in blocked]
            raise SkillCompilationError(
                f"FR-017: {len(blocked)} DecisionRule(s) con requires_review=True "
                f"bloquean la firma del SkillPackage: {ids}"
            )


def to_skill_md(
    *,
    skill_name: str,
    description: str,
    narrative: VoiceNarrative,
    decision_rules: list[DecisionRule],
    version: str = "1",
) -> "SkillMdDocument":
    """Convert teaching-path artefacts to the unified SKILL.md format.

    Produces a SkillMdDocument that can be serialized and signed identically
    to the autonomous path output, closing the convergence gap (F3).

    Args:
        skill_name:     Slug name for the skill (filesystem-safe).
        description:    One-liner description (from the narrative or operator).
        narrative:      VoiceNarrative produced by the training session.
        decision_rules: DecisionRules compiled from the session.
        version:        Semantic version string (default "1").

    Returns:
        SkillMdDocument in canonical SKILL.md format.
    """
    from hermes.training.domain.skill_md_document import SkillMdDocument  # noqa: PLC0415

    procedure = _build_procedure_from_rules(decision_rules)
    when = _build_when_from_narrative(narrative)

    body = (
        f"## When\n{when}\n\n"
        f"## Procedure\n{procedure}\n\n"
        f"## Pitfalls\n- (none documented at training time)\n\n"
        f"## Verification\n- Verify the expected outcome matches the training session result.\n"
    )

    return SkillMdDocument(
        name=skill_name,
        description=description,
        version=version,
        body=body,
        metadata={"source": "teaching", "rule_count": len(decision_rules)},
    )


def _build_when_from_narrative(narrative: VoiceNarrative) -> str:
    """Derive trigger conditions from voice narrative fragments."""
    transcripts = [
        f.transcript.strip()
        for f in narrative.fragments
        if f.transcript and f.is_usable_for_rule_inference()
    ]
    if not transcripts:
        return "- (trigger conditions derived from operator demonstration)"
    return "\n".join(f"- {t}" for t in transcripts[:5])


def _build_procedure_from_rules(rules: list[DecisionRule]) -> str:
    """Serialize decision rules as numbered procedure steps."""
    if not rules:
        return "1. (steps captured during training session)"
    lines = []
    for i, rule in enumerate(rules, 1):
        action_desc = rule.action or "(captured action)"
        if rule.categorical_markers:
            markers = ", ".join(sorted(rule.categorical_markers))
            lines.append(f"{i}. {action_desc} [markers: {markers}]")
        else:
            lines.append(f"{i}. {action_desc}")
    return "\n".join(lines)


def _get_runtime_version() -> str:
    try:
        return importlib.metadata.version("hermes-runtime")
    except importlib.metadata.PackageNotFoundError:
        return "dev"
