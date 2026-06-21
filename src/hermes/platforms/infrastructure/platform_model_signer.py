"""PlatformModelSigner — sign and verify PlatformModel signatures (T020).

Reuses AuditHashChainSigner's HMAC-SHA-256 primitive to sign the model's
identity + per-zone hashes. Does NOT roll custom crypto.

The signature covers: {model_id, version, tenant_id, origin_attribution,
content_hash, per_zone_hashes} as required by the data model invariant.

Verification is offline-capable: only the signing_key is needed.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from hermes.platforms.domain.platform_model import PlatformModel
from hermes.platforms.domain.value_objects import PlatformModelSignature


class InvalidModelSignature(ValueError):
    """Model signature verification failed — fail-closed (CTRL-5)."""


class PlatformModelSigner:
    """Signs and verifies PlatformModel content hashes.

    Args:
        signing_key: ≥32 bytes symmetric secret (same store as audit key).
                     In production: loaded from LUKS/TPM2, NEVER hardcoded.
    """

    def __init__(self, *, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("signing_key must be at least 32 bytes")
        self._key = signing_key

    def sign(self, model: PlatformModel) -> PlatformModelSignature:
        """Compute and return a PlatformModelSignature for the model.

        The signature covers identity + all zone hashes to allow granular
        additive updates (FR-022, FR-028).
        """
        per_zone_hashes = tuple(
            z.zone_hash.hex_digest for z in sorted(model.zones, key=lambda z: z.zone_id)
        )
        content_hash = self._compute_content_hash(model, per_zone_hashes)
        origin_attribution = str(model.origin)
        payload = self._build_payload(
            model_id=str(model.platform_model_id),
            version=model.version.number,
            tenant_id=model.tenant_id,
            origin_attribution=origin_attribution,
            content_hash=content_hash,
            per_zone_hashes=per_zone_hashes,
        )
        signature_hex = self._hmac_sign(payload)
        return PlatformModelSignature(
            platform_model_id=str(model.platform_model_id),
            version=model.version.number,
            tenant_id=model.tenant_id,
            origin_attribution=origin_attribution,
            content_hash=content_hash,
            per_zone_hashes=per_zone_hashes,
            signature_hex=signature_hex,
        )

    def verify(self, signature: PlatformModelSignature) -> None:
        """Verify a stored signature. Raises InvalidModelSignature on mismatch.

        Fail-closed: a mismatch means the model was tampered with (CTRL-5).
        """
        payload = self._build_payload(
            model_id=signature.platform_model_id,
            version=signature.version,
            tenant_id=signature.tenant_id,
            origin_attribution=signature.origin_attribution,
            content_hash=signature.content_hash,
            per_zone_hashes=signature.per_zone_hashes,
        )
        expected_hex = self._hmac_sign(payload)
        if not hmac.compare_digest(expected_hex, signature.signature_hex):
            raise InvalidModelSignature(
                f"Signature mismatch for model {signature.platform_model_id} "
                f"v{signature.version} — potential tampering (CTRL-5)"
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compute_content_hash(
        self, model: PlatformModel, per_zone_hashes: tuple[str, ...]
    ) -> str:
        content = {
            "model_id": str(model.platform_model_id),
            "version": model.version.number,
            "tenant_id": model.tenant_id,
            "site_ref": model.site_ref,
            "area_ids": sorted(a.area_id for a in model.areas),
            "entity_ids": sorted(e.entity_id for e in model.entities),
            "rule_ids": sorted(r.rule_id for r in model.house_rules),
            "zone_hashes": sorted(per_zone_hashes),
        }
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_payload(
        *,
        model_id: str,
        version: int,
        tenant_id: str,
        origin_attribution: str,
        content_hash: str,
        per_zone_hashes: tuple[str, ...],
    ) -> bytes:
        payload = {
            "model_id": model_id,
            "version": version,
            "tenant_id": tenant_id,
            "origin_attribution": origin_attribution,
            "content_hash": content_hash,
            "per_zone_hashes": sorted(per_zone_hashes),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _hmac_sign(self, payload: bytes) -> str:
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()
