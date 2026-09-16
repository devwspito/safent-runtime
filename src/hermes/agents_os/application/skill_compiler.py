"""SkillPackage value objects + signer/verifier for skill_replay / intent_router.

Historically compiled a TrainingSession (the teach-by-browser demonstration
capture) into a signed SkillPackage. Teach-by-browser was retired 10-sep-2026
(specs/025-safent-repaso/retirada-ensenar.md, owner decision) — no production
caller builds new packages today (skill_replay, intent_router and the
SQLite/Postgres skill-package repos only ever consume already-persisted
packages), so ``compile_from_steps`` takes step data directly instead of a
TrainingSession. The HMAC construction stays in one place, next to
``verify()``, rather than duplicated into test fixtures.

An artefact is a ``SkillPackage``:

  - tenant_id, skill_id, version (monotonic)
  - ordered list of steps per surface_kind (replay-ready)
  - voice_caption aggregated as narrative "intent"
  - signature_hex over the canonicalized bundle (HMAC-SHA-256)
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

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


class SkillCompiler:
    """Builds and verifies signed SkillPackage bundles (HMAC-SHA-256)."""

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
                           from each step's voice_caption.
        """
        if len(signing_key) < 32:
            raise ValueError("signing_key debe tener al menos 32 bytes")
        self._key = signing_key
        self._clock = clock
        self._extra_caption = extra_caption

    def compile_from_steps(
        self,
        *,
        tenant_id: UUID,
        skill_id: str,
        steps: Sequence[tuple[SurfaceKind, dict, str | None]],
        version: int,
    ) -> SkillPackage:
        """Build and sign a SkillPackage from (surface_kind, payload, caption) steps."""
        if version < 1:
            raise ValueError("version debe ser >= 1")
        if not steps:
            raise SkillCompilationError("no steps to compile")

        bundle = _bundle_steps_by_surface(steps)
        intent = (
            self._extra_caption
            if self._extra_caption is not None
            else _intent_caption_from(steps)
        )

        surface_kinds = frozenset({sk for sk, _payload, _caption in steps})

        package_id = uuid4()
        canonical_payload = _canonical_json(
            _package_signing_fields(
                package_id=package_id,
                tenant_id=tenant_id,
                skill_id=skill_id,
                version=version,
                surface_kinds=surface_kinds,
                intent_caption=intent,
                bundle=bundle,
                source_training_session_id=package_id,
            )
        )
        signature = hmac.new(
            self._key, canonical_payload, hashlib.sha256
        ).hexdigest()

        return SkillPackage(
            package_id=package_id,
            tenant_id=tenant_id,
            skill_id=skill_id,
            version=version,
            state=SkillPackageState.SIGNED,
            surface_kinds=surface_kinds,
            cross_domain=len(surface_kinds) > 1,
            steps_by_surface_kind=bundle,
            intent_caption=intent,
            source_training_session_id=package_id,
            created_at=self._clock(),
            signature_hex=signature,
        )

    def verify(self, package: SkillPackage) -> bool:
        """Recompone el canonical payload y verifica HMAC."""
        canonical_payload = _canonical_json(
            _package_signing_fields(
                package_id=package.package_id,
                tenant_id=package.tenant_id,
                skill_id=package.skill_id,
                version=package.version,
                surface_kinds=package.surface_kinds,
                intent_caption=package.intent_caption,
                bundle=package.steps_by_surface_kind,
                source_training_session_id=package.source_training_session_id,
            )
        )
        expected = hmac.new(
            self._key, canonical_payload, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, package.signature_hex)


def _package_signing_fields(
    *,
    package_id: UUID,
    tenant_id: UUID,
    skill_id: str,
    version: int,
    surface_kinds: frozenset[SurfaceKind],
    intent_caption: str,
    bundle: dict[str, list[SkillStep]],
    source_training_session_id: UUID,
) -> dict:
    return {
        "package_id": str(package_id),
        "tenant_id": str(tenant_id),
        "skill_id": skill_id,
        "version": version,
        "surface_kinds": sorted(sk.value for sk in surface_kinds),
        "cross_domain": len(surface_kinds) > 1,
        "intent_caption": intent_caption,
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
        "source_training_session_id": str(source_training_session_id),
    }


def _intent_caption_from(
    steps: Sequence[tuple[SurfaceKind, dict, str | None]],
) -> str:
    """Agrega los voice_captions en un texto narrativo."""
    parts = [caption.strip() for _sk, _payload, caption in steps if caption]
    return " · ".join(parts)


def _bundle_steps_by_surface(
    steps: Sequence[tuple[SurfaceKind, dict, str | None]],
) -> dict[str, list[SkillStep]]:
    bucket: dict[str, list[SkillStep]] = {}
    for i, (surface_kind, action_payload, _caption) in enumerate(steps):
        bucket.setdefault(surface_kind.value, []).append(
            SkillStep(
                sequence_index=i,
                surface_kind=surface_kind,
                action_payload=dict(action_payload),
            )
        )
    return bucket
