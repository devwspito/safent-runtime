"""SkillCompiler — convierte TrainingSession SIGNED en SkillPackage.

Spec 003 FR-026, FR-031 — el output de US2. Un SkillPackage es un
artefacto inmutable firmado HMAC-SHA-256 con:

  - tenant_id, skill_id, version (monotónico)
  - lista ordenada de steps por surface_kind (replay-ready)
  - voice_caption agregada como "intent narrativo"
  - hash del audio original (no el audio en sí — solo prueba de
    cadena de custodia)
  - signature_hex sobre todo el bundle canónico

El SkillPackage se persiste en `skill_packages` (migration 018). El
runtime lo replay vía SurfaceAdapter sin pasar por LLM (research §10
route memorization).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSession,
    TrainingSessionState,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind


class SkillCompilationError(RuntimeError):
    pass


class SkillPackageState(StrEnum):
    DRAFT = "draft"
    SIGNED = "signed"
    DEPRECATED = "deprecated"


@dataclass(frozen=True, slots=True)
class SkillStep:
    """Step replay-ready (sin audio crudo)."""

    sequence_index: int
    surface_kind: SurfaceKind
    action_payload: dict


@dataclass(frozen=True, slots=True)
class SkillPackage:
    """Artefacto firmado e inmutable."""

    package_id: UUID
    tenant_id: UUID
    skill_id: str
    version: int
    state: SkillPackageState
    surface_kinds: frozenset[SurfaceKind]
    cross_domain: bool
    steps_by_surface_kind: dict[str, list[SkillStep]]
    intent_caption: str  # narrativa agregada de voice_captions
    source_training_session_id: UUID
    created_at: datetime
    signature_hex: str


def _canonical_json(obj) -> bytes:
    """Mismo canonical encoder que audit chain — orden+espacios fijos."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _intent_caption_from(session: TrainingSession) -> str:
    """Agrega los voice_captions en un texto narrativo."""
    parts = [s.voice_caption.strip() for s in session.steps if s.voice_caption]
    return " · ".join(parts)


def _bundle_steps_by_surface(
    session: TrainingSession,
) -> dict[str, list[SkillStep]]:
    bucket: dict[str, list[SkillStep]] = {}
    for s in session.steps:
        bucket.setdefault(s.surface_kind.value, []).append(
            SkillStep(
                sequence_index=s.sequence_index,
                surface_kind=s.surface_kind,
                action_payload=dict(s.action_payload),
            )
        )
    return bucket


class SkillCompiler:
    """Compila una TrainingSession SIGNED en SkillPackage firmado."""

    def __init__(
        self,
        *,
        signing_key: bytes,
        clock=lambda: datetime.now(tz=UTC),
        extra_caption: str | None = None,
    ) -> None:
        """
        Args:
            extra_caption: when provided, overrides the intent_caption derived
                           from step.voice_caption.  Used by compile_and_persist
                           to bridge coordinator.collected_voice_captions() into
                           the signed package (step-level captions are None when
                           the coordinator stores them separately).
        """
        if len(signing_key) < 32:
            raise ValueError("signing_key debe tener al menos 32 bytes")
        self._key = signing_key
        self._clock = clock
        self._extra_caption = extra_caption

    def compile(
        self,
        *,
        session: TrainingSession,
        version: int,
    ) -> SkillPackage:
        from hermes.agents_os.application.training_session_orchestrator import (  # noqa: PLC0415
            VoiceCaptureRequired,
        )

        if session.state != TrainingSessionState.SIGNED:
            raise SkillCompilationError(
                f"compile requiere SIGNED, está {session.state}"
            )
        if version < 1:
            raise ValueError("version debe ser >= 1")
        if not session.steps:
            raise SkillCompilationError("session sin steps")

        bundle = _bundle_steps_by_surface(session)
        # Prefer the externally-supplied caption (from coordinator.collected_voice_captions)
        # over the per-step captions, which are None in the coordinator flow.
        if self._extra_caption is not None:
            intent = self._extra_caption
        else:
            intent = _intent_caption_from(session)

        # Enforce invariant: voice_required session must have non-empty intent.
        if getattr(session, "voice_required", False) and not intent.strip():
            raise VoiceCaptureRequired(
                "session.voice_required pero intent_caption está vacío"
            )

        surface_kinds = frozenset({s.surface_kind for s in session.steps})

        package_id = uuid4()
        canonical_payload = _canonical_json(
            {
                "package_id": str(package_id),
                "tenant_id": str(session.tenant_id),
                "skill_id": session.skill_id,
                "version": version,
                "surface_kinds": sorted(sk.value for sk in surface_kinds),
                "cross_domain": len(surface_kinds) > 1,
                "intent_caption": intent,
                "steps_by_surface_kind": {
                    sk: [
                        {
                            "sequence_index": step.sequence_index,
                            "surface_kind": step.surface_kind.value,
                            "action_payload": step.action_payload,
                        }
                        for step in steps
                    ]
                    for sk, steps in bundle.items()
                },
                "source_training_session_id": str(session.session_id),
            }
        )
        signature = hmac.new(
            self._key, canonical_payload, hashlib.sha256
        ).hexdigest()

        return SkillPackage(
            package_id=package_id,
            tenant_id=session.tenant_id,
            skill_id=session.skill_id,
            version=version,
            state=SkillPackageState.SIGNED,
            surface_kinds=surface_kinds,
            cross_domain=len(surface_kinds) > 1,
            steps_by_surface_kind=bundle,
            intent_caption=intent,
            source_training_session_id=session.session_id,
            created_at=self._clock(),
            signature_hex=signature,
        )

    def verify(self, package: SkillPackage) -> bool:
        """Recompone el canonical payload y verifica HMAC."""
        canonical_payload = _canonical_json(
            {
                "package_id": str(package.package_id),
                "tenant_id": str(package.tenant_id),
                "skill_id": package.skill_id,
                "version": package.version,
                "surface_kinds": sorted(
                    sk.value for sk in package.surface_kinds
                ),
                "cross_domain": package.cross_domain,
                "intent_caption": package.intent_caption,
                "steps_by_surface_kind": {
                    sk: [
                        {
                            "sequence_index": step.sequence_index,
                            "surface_kind": step.surface_kind.value,
                            "action_payload": step.action_payload,
                        }
                        for step in steps
                    ]
                    for sk, steps in package.steps_by_surface_kind.items()
                },
                "source_training_session_id": str(
                    package.source_training_session_id
                ),
            }
        )
        expected = hmac.new(
            self._key, canonical_payload, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, package.signature_hex)
