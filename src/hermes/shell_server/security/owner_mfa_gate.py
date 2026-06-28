"""Gate único de MFA del dueño para cambios de postura de seguridad (web).

Antes había DOS helpers `_require_owner_mfa` casi idénticos (egress_api +
policies_api), cada uno verificando TOTP en su propio endpoint. Esto los colapsa
en una sola superficie de enforcement (lección del red-team 2026-06-19 finding 3:
el enforcement debe ser estructural, no replicable por endpoint).

Modelo TOTP-only (decisión del dueño 2026-06-24). Fail-closed: sin enrolar o
código malo → rechaza. El agente enjaulado no puede acuñar el TOTP (secreto 0600
solo-dueño), así que no puede abrir su propia jaula.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

from hermes.shell_server.security.mfa import MfaStore, ProtectionLevel

logger = logging.getLogger("hermes.shell_server.security.owner_mfa_gate")


def require_owner_mfa(mfa_store: MfaStore, totp: str, *, action: str) -> None:
    """Exige el TOTP del dueño para una acción de postura de seguridad.

    `action` es la etiqueta humana de la acción (p.ej. "cambiar el modo de red")
    que se interpola en el mensaje de error. Lanza HTTPException 403 (sin enrolar)
    o 401 (código inválido).
    """
    if not mfa_store.is_enrolled():
        raise HTTPException(
            status_code=403,
            detail={
                "code": "mfa_not_enrolled",
                "message": f"Configura el MFA antes de {action}.",
            },
        )
    ok, reason = mfa_store.verify(level=ProtectionLevel.MFA, totp=totp or "")
    if not ok:
        logger.warning(
            "hermes.mfa.owner_gate_denied action=%r reason=%s", action, reason
        )
        raise HTTPException(
            status_code=401,
            detail={
                "code": reason,
                "message": f"{action[:1].upper()}{action[1:]} exige tu código MFA.",
            },
        )
