"""Value objects for the Platforms bounded context (T008).

All types are immutable (frozen dataclasses or StrEnum).
Domain layer — zero framework / infra / HTTP dependencies.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


# ---------------------------------------------------------------------------
# Identity value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatformModelId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("PlatformModelId cannot be empty")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ModelVersion:
    """Monotonically increasing integer version of a PlatformModel."""

    number: int

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("ModelVersion.number must be >= 1")

    def next(self) -> ModelVersion:
        return ModelVersion(self.number + 1)

    def __str__(self) -> str:
        return str(self.number)


@dataclass(frozen=True, slots=True)
class DomainName:
    """Operator-given name for a platform area or business entity (no PII)."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("DomainName cannot be empty")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class NavigationPath:
    """How to reach a PlatformArea (URL fragment, menu sequence, etc.)."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("NavigationPath cannot be empty")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ActionRef:
    """Reference to an action available in a PlatformArea."""

    name: str

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("ActionRef.name cannot be empty")


@dataclass(frozen=True, slots=True)
class EntityRelationship:
    """Narrated relationship from one BusinessEntity to another."""

    target_entity_id: str
    description: str

    def __post_init__(self) -> None:
        if not self.target_entity_id:
            raise ValueError("EntityRelationship.target_entity_id cannot be empty")


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class LandmarkKind(StrEnum):
    GLOBAL_SEARCH = "global_search"
    CREATE_BUTTON = "create_button"
    SECTION_ENTRY = "section_entry"
    NAVIGATION_MENU = "navigation_menu"
    OTHER = "other"


class HouseRuleKind(StrEnum):
    NEVER_TOUCH = "never_touch"
    ALWAYS_BEFORE = "always_before"
    REQUIRED_STEP = "required_step"


class LifecycleState(StrEnum):
    PROVISIONAL = "provisional"
    APRENDIDA = "aprendida"
    HABILITADA = "habilitada"
    STALE = "stale"
    DEPRECADA = "deprecada"


class TourOrigin(StrEnum):
    GUIDED = "guided"
    AUTONOMOUS = "autonomous"


# ---------------------------------------------------------------------------
# ZoneHash — deterministic content hash
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ZoneHash:
    """SHA-256 hex digest of a zone's canonical content.

    Invariant: same content → same hash; different content → different hash.
    Constructed via ZoneHash.compute(...) to enforce determinism.
    """

    hex_digest: str

    def __post_init__(self) -> None:
        if len(self.hex_digest) != 64:
            raise ValueError("ZoneHash must be a 64-char SHA-256 hex digest")

    @classmethod
    def compute(cls, content: dict) -> ZoneHash:
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls(hex_digest=digest)

    def __str__(self) -> str:
        return self.hex_digest


# ---------------------------------------------------------------------------
# PlatformModelSignature — covers {model_id, version, tenant_id,
#   origin_attribution, content_hash, per_zone_hashes} (FR-028)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatformModelSignature:
    """Cryptographic signature over the model identity + content.

    Verifiable offline via platform_model_signer.verify().
    """

    platform_model_id: str
    version: int
    tenant_id: str
    origin_attribution: str
    content_hash: str
    per_zone_hashes: tuple[str, ...]
    signature_hex: str

    def __post_init__(self) -> None:
        if not self.signature_hex:
            raise ValueError("PlatformModelSignature.signature_hex cannot be empty")
        if not self.content_hash:
            raise ValueError("PlatformModelSignature.content_hash cannot be empty")


# ---------------------------------------------------------------------------
# CapabilityRef — kind ∈ {platform, skill, mcp} + id + version
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityRef:
    """Reference to a capability in the global library.

    Invariant: kind ∈ {platform, skill, mcp}; integrations NOT representable.
    `mcp` referencia un servidor MCP configurado (ServerSlug como capability_id)
    — skills y MCP son GLOBALES; el binding los asigna a un agente concreto
    (visión Agents 2026-06-10: editar un agente = elegir sus skills y sus MCP).
    """

    kind: Literal["platform", "skill", "mcp"]
    capability_id: str
    version: str

    def __post_init__(self) -> None:
        if self.kind not in ("platform", "skill", "mcp"):
            raise ValueError(f"CapabilityRef.kind must be 'platform', 'skill' or 'mcp', got {self.kind!r}")
        if not self.capability_id or not self.capability_id.strip():
            raise ValueError("CapabilityRef.capability_id cannot be empty")
        if not self.version or not self.version.strip():
            raise ValueError("CapabilityRef.version cannot be empty")

    def __str__(self) -> str:
        return f"{self.kind}:{self.capability_id}@{self.version}"


# ---------------------------------------------------------------------------
# TeachingModality — system × narration channel (FR-001b)
# ---------------------------------------------------------------------------


class TeachingModality(StrEnum):
    """Encodes teaching system × narration channel.

    system ∈ {demonstrating, describing}
    narration ∈ {audio, text}

    demonstrating + audio  → video_audio  (recommended: demo + voice)
    demonstrating + text   → video_text   (demo + typed narration)
    describing   + audio   → audio_only   (voice description, no demo)
    describing   + text    → text_only    (typed description, no demo)

    Invariants enforced in PlatformLearningTour and TeachingSession:
    - demonstrating implies capture of demonstration (video track present).
    - describing implies no demo; knowledge starts as reasoned instruction.
    - audio narration is transcribed by Whisper → text BEFORE any inference
      (Principio III), so at domain level narration is always text after intake.
    - autonomous exploration implies text_only (no human narration channel).
    """

    VIDEO_AUDIO = "video_audio"
    VIDEO_TEXT = "video_text"
    AUDIO_ONLY = "audio_only"
    TEXT_ONLY = "text_only"

    @property
    def is_demonstrating(self) -> bool:
        return self in (TeachingModality.VIDEO_AUDIO, TeachingModality.VIDEO_TEXT)

    @property
    def is_describing(self) -> bool:
        return self in (TeachingModality.AUDIO_ONLY, TeachingModality.TEXT_ONLY)

    @property
    def has_audio_narration(self) -> bool:
        return self in (TeachingModality.VIDEO_AUDIO, TeachingModality.AUDIO_ONLY)

    @property
    def has_video(self) -> bool:
        return self in (TeachingModality.VIDEO_AUDIO, TeachingModality.VIDEO_TEXT)
