"""T034 — ApprovalGrant VO.

Objeto verificable que el supervisor emite al aprobar una propuesta HIGH.
Inmutable; contiene todos los datos necesarios para que el broker reconstruya
y verifique el token HMAC sin tocar la BD.

threat-model CTRL-1:
  - Ligado a (proposal_id, capability, expiry, nonce).
  - single-use: el token se marca consumido en HitlApprovalMinter.
  - No contiene la clave de firma — solo el token opaco.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """Evidencia de que un supervisor humano aprobó esta propuesta.

    Attributes:
        proposal_id:  Propuesta aprobada (ligado en el HMAC).
        capability:   Capability que se autorizó (p.ej. "terminal").
        expiry:       Instante UTC en que expira el token.
        nonce:        Nonce aleatorio por-proposal (anti-replay).
        token:        Token HMAC opaco listo para pasar al broker.
    """

    proposal_id: UUID
    capability: str
    expiry: datetime
    nonce: str
    token: str

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """True si el token ya ha superado su tiempo de validez."""
        reference = now if now is not None else datetime.now(tz=UTC)
        return reference >= self.expiry
