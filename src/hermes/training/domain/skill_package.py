"""SkillPackage (data-model §8, FR-013/014/015/017).

Artefacto compilado y firmado que encapsula una skill aprendida durante
training. Inmutable una vez firmado.

Contiene:
- ``replay_script_id`` (referencia a entidad heredada de spec 001).
- ``voice_narrative_id`` (agregado de transcripts).
- ``decision_rule_ids`` (lista ordenada).
- ``state`` (DRAFT / VALIDATED / AUTONOMOUS / PENDING_RECONFIRMATION / ARCHIVED).
- ``signature_hex`` (HMAC-SHA256 sobre el conjunto canonicalizado).
- ``predecessor_package_id`` (lineage para re-entrenamientos y self-healing).

Invariantes (constitución y FRs):
- FR-013: una SkillPackage SIEMPRE contiene replay_script + narrative + rules.
- FR-014: payload templates solo con placeholders tokenizados; NUNCA PII en cleartext.
- FR-015: firma incluye tenant_id + operator_id + hash replay + hash rules + ts.
- FR-017: si alguna regla está ``requires_review=True`` → ``state == DRAFT`` y
  la firma NO se emite. ``can_be_signed()`` lo verifica.
- FR-021/022: el flujo state machine lo gobierna ``skill_state.assert_transition``.

La construcción real (calcular el HMAC) la hace el ``SkillSigner``
(infrastructure). Este domain VO solo modela los datos y los invariantes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from hermes.training.domain.skill_state import SkillState


@dataclass(frozen=True, slots=True)
class SkillPackage:
    """Inmutable. Para mutar = crear una versión sucesora via ``with_*``."""

    package_id: UUID = field(default_factory=uuid4)
    skill_id: UUID = field(default_factory=uuid4)
    skill_version: int = 1
    tenant_id: UUID | None = None
    site_id: str = ""
    flow_id: str = ""
    replay_script_id: UUID | None = None
    voice_narrative_id: UUID | None = None
    decision_rule_ids: tuple[UUID, ...] = ()
    state: SkillState = SkillState.DRAFT
    signature_hex: str = ""
    signing_key_id: str = ""
    runtime_version: str = ""
    compiled_by_operator_id: UUID | None = None
    predecessor_package_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    validated_at: datetime | None = None
    validated_by_operator_id: UUID | None = None
    promoted_at: datetime | None = None
    promoted_by_admin_id: UUID | None = None
    # FR-015 addendum: SHA-256 hex over the executable content (decision rule
    # patterns, replay steps, argument schema) computed at compile time.
    # Included in the HMAC payload so that mutating the artefacts invalidates
    # the signature even if the UUIDs remain the same.
    # Empty string is allowed only before compilation (DRAFT pre-compile).
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.skill_version < 1:
            raise ValueError("skill_version debe ser >= 1")
        if self.signature_hex and len(self.signature_hex) != 64:
            raise ValueError(
                "signature_hex debe ser SHA-256 hex (64 chars) o vacío"
            )
        if self.content_hash and len(self.content_hash) != 64:
            raise ValueError(
                "content_hash debe ser SHA-256 hex (64 chars) o vacío"
            )

    def is_signed(self) -> bool:
        return bool(self.signature_hex)

    def can_be_signed(
        self, *, decision_rules_requiring_review: int
    ) -> bool:
        """FR-017: no se firma si hay reglas pendientes de revisión."""
        if decision_rules_requiring_review > 0:
            return False
        if self.state != SkillState.DRAFT:
            return False
        if self.replay_script_id is None:
            return False
        return True
