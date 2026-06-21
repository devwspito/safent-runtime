"""SkillSigner — firma HMAC-SHA256 sobre SkillPackage DRAFT (T100, FR-015).

Input al hash (canonicalizado con JSON sort_keys=True):
  replay_script_id + decision_rule_ids + voice_narrative_id + content_hash
  + atribuciones + timestamp

El campo content_hash es un SHA-256 hex sobre el CONTENIDO EJECUTABLE real
(patrones de decision rules, steps del replay, argument schema). El compilador
lo calcula y embebe en SkillPackage antes de llamar a SkillSigner.sign().
Esto garantiza que mutar un artefacto referenciado invalida la firma aunque
los UUIDs no cambien (FR-015 addendum).

La clave de firma viene de un KMS port abstracto (no acoplado a Vault aquí).
Verificable independientemente con verify_skill_signature().
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from hermes.training.domain.skill_package import SkillPackage

logger = logging.getLogger(__name__)


class SigningKeyError(RuntimeError):
    """No se pudo obtener la clave de firma del KMS."""


class SignatureVerificationError(RuntimeError):
    """La firma del SkillPackage no es válida (FR-015)."""


class KmsSigningKeyPort(Protocol):
    """Puerto abstracto del KMS para firmar SkillPackages."""

    async def get_signing_key(
        self,
        *,
        tenant_id: UUID,
        key_id: str,
    ) -> bytes:
        """Devuelve el material de clave HMAC-SHA256 (32 bytes).

        Levanta SigningKeyError si el key_id no existe o el tenant no tiene acceso.
        """
        ...


def build_canonical_payload(package: SkillPackage) -> bytes:
    """Serialización determinista para firma (FR-015 + addendum).

    Cubre: replay_script_id, decision_rule_ids, voice_narrative_id,
    content_hash (SHA-256 del contenido ejecutable real), tenant_id,
    compiled_by_operator_id, created_at.

    El campo content_hash vincula la firma al CONTENIDO de los artefactos
    referenciados, no solo a sus UUIDs. Mutar un decision rule o replay step
    invalida la firma aunque los UUIDs no cambien.

    Un content_hash vacío se rechaza en sign() para evitar firmar paquetes
    sin contenido verificable.

    sort_keys=True garantiza orden determinista independiente de la inserción.
    """
    payload = {
        "replay_script_id": str(package.replay_script_id),
        "decision_rule_ids": sorted(str(r) for r in package.decision_rule_ids),
        "voice_narrative_id": str(package.voice_narrative_id),
        "content_hash": package.content_hash,
        "tenant_id": str(package.tenant_id),
        "compiled_by_operator_id": str(package.compiled_by_operator_id),
        "created_at": package.created_at.isoformat(),
        "runtime_version": package.runtime_version,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


class SkillSigner:
    """Firma y verifica SkillPackages con HMAC-SHA256."""

    def __init__(self, *, kms: KmsSigningKeyPort) -> None:
        self._kms = kms

    async def sign(
        self,
        *,
        package: SkillPackage,
        signing_key_id: str,
    ) -> SkillPackage:
        """Firma el SkillPackage y devuelve una nueva instancia con signature_hex.

        FR-015: la firma cubre replay_script_id + decision_rule_ids +
        voice_narrative_id + content_hash + atribuciones + timestamp.

        Raises:
            SigningKeyError: si tenant_id es None o content_hash está vacío.
        """
        if package.tenant_id is None:
            raise SigningKeyError("tenant_id no puede ser None para firmar")
        if not package.content_hash:
            raise SigningKeyError(
                f"SkillPackage {package.package_id}: content_hash vacío — "
                "el compilador debe calcular el hash del contenido ejecutable "
                "antes de firmar (FR-015 addendum). "
                "Usar SkillCompiler.compute_content_hash() para generarlo."
            )

        key_bytes = await self._kms.get_signing_key(
            tenant_id=package.tenant_id,
            key_id=signing_key_id,
        )
        canonical = build_canonical_payload(package)
        signature = _compute_hmac(key_bytes, canonical)

        signed = replace(
            package,
            signature_hex=signature,
            signing_key_id=signing_key_id,
        )

        logger.info(
            "skill_package_signed",
            extra={
                "tenant_id": str(package.tenant_id),
                "package_id": str(package.package_id),
                "signing_key_id": signing_key_id,
            },
        )
        return signed


async def verify_skill_signature(
    *,
    package: SkillPackage,
    kms: KmsSigningKeyPort,
) -> None:
    """Verifica HMAC-SHA256. Levanta SignatureVerificationError si es inválida.

    Verificable independientemente (FR-015).
    """
    if not package.signature_hex:
        raise SignatureVerificationError("SkillPackage no tiene firma (signature_hex vacío)")
    if package.tenant_id is None:
        raise SignatureVerificationError("tenant_id requerido para verificar firma")

    key_bytes = await kms.get_signing_key(
        tenant_id=package.tenant_id,
        key_id=package.signing_key_id,
    )
    canonical = build_canonical_payload(package)
    expected = _compute_hmac(key_bytes, canonical)

    if not hmac.compare_digest(expected, package.signature_hex):
        raise SignatureVerificationError(
            f"Firma inválida para SkillPackage {package.package_id} "
            f"(tenant {package.tenant_id})"
        )


def _compute_hmac(key: bytes, message: bytes) -> str:
    return hmac.new(key, message, hashlib.sha256).hexdigest()
