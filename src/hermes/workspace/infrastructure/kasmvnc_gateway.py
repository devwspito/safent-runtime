"""KasmVncGatewayAdapter — fallback gateway VNC (T084).

Adapter alternativo que activa KasmVNC cuando Selkies no negocia ICE en N
segundos (research §1). Misma interfaz que ``SelkiesGatewayAdapter``.

``supports_audio = False``: KasmVNC tiene soporte de audio frágil; en este
adapter se marca explícitamente como no soportado. Si el caller solicita
``AUDIO_INBOUND`` o ``AUDIO_OUTBOUND``, se lanza ``GatewayCapabilityUnavailable``
(fail-closed, constitución IV).

Lazy-import: cualquier binding de KasmVNC vive dentro de los métodos que lo
necesiten, no en el nivel del módulo.

FR-001..005, FR-038. Activación automática desde ``RemoteDesktopSupervisor``
(T085) cuando Selkies cae.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from hermes.workspace.domain.ports.remote_desktop_gateway_port import (
    GatewayCapability,
    GatewayCapabilityUnavailable,
    GatewaySessionTicket,
    GatewayTokenClaim,
    GatewayTokenExpired,
    GatewayTokenInvalid,
)

logger = logging.getLogger(__name__)

__all__ = ["KasmVncConfig", "KasmVncConnectionStatus", "KasmVncGatewayAdapter"]

# KasmVNC soporta sólo video + clipboard.
_KASMVNC_SUPPORTED: frozenset[GatewayCapability] = frozenset(
    {
        GatewayCapability.VIDEO,
        GatewayCapability.CLIPBOARD_INBOUND,
        GatewayCapability.CLIPBOARD_OUTBOUND,
    }
)

# Audio explícitamente NO soportado.
_AUDIO_CAPABILITIES: frozenset[GatewayCapability] = frozenset(
    {GatewayCapability.AUDIO_INBOUND, GatewayCapability.AUDIO_OUTBOUND}
)


class KasmVncConnectionStatus(StrEnum):
    STARTING = "starting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class KasmVncConfig:
    """Configuración del adapter KasmVNC inyectada en boot."""

    gateway_base_url: str
    wss_base_url: str
    signing_key: bytes
    viewport_width: int = 1280
    viewport_height: int = 720
    kasmvnc_socket: str = "/var/run/hermes/sockets/kasmvnc.sock"


class KasmVncGatewayAdapter:
    """Adapter KasmVNC. Cumple ``RemoteDesktopGatewayPort``.

    Se activa automáticamente por el ``RemoteDesktopSupervisor`` como fallback.
    """

    supports_audio: bool = False

    def __init__(self, config: KasmVncConfig) -> None:
        self._cfg = config
        self._active_tokens: dict[str, GatewayTokenClaim] = {}
        self._status: dict[UUID, KasmVncConnectionStatus] = {}

    # ------------------------------------------------------------------
    # RemoteDesktopGatewayPort
    # ------------------------------------------------------------------

    async def issue_session_ticket(
        self,
        *,
        workspace_id: UUID,
        tenant_id: UUID,
        human_operator_id: UUID,
        ttl_seconds: int,
        capabilities: frozenset[GatewayCapability],
    ) -> GatewaySessionTicket:
        """Acuña ticket KasmVNC. Rechaza capabilities de audio (fail-closed)."""
        audio_requested = capabilities & _AUDIO_CAPABILITIES
        if audio_requested:
            raise GatewayCapabilityUnavailable(
                f"KasmVNC no soporta audio ({sorted(str(c) for c in audio_requested)}). "
                "Usa SelkiesGatewayAdapter para audio bidireccional."
            )
        unsupported = capabilities - _KASMVNC_SUPPORTED
        if unsupported:
            raise GatewayCapabilityUnavailable(
                f"KasmVNC no soporta: {sorted(str(c) for c in unsupported)}"
            )

        now = datetime.now(tz=UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)
        nonce = secrets.token_hex(16)
        token = self._mint_token(
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
            nonce=nonce,
            expires_at=expires_at,
        )
        claim = GatewayTokenClaim(
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
            issued_at=now,
            expires_at=expires_at,
            capabilities=capabilities,
        )
        self._active_tokens[token] = claim
        self._status[workspace_id] = KasmVncConnectionStatus.STARTING

        logger.info(
            "kasmvnc_gateway.ticket_issued",
            extra={
                "workspace_id": str(workspace_id),
                "tenant_id": str(tenant_id),
            },
        )

        ticket_url = f"{self._cfg.gateway_base_url}/connect?nonce={nonce}"
        wss_url = f"{self._cfg.wss_base_url}/ws"
        return GatewaySessionTicket(
            ticket_url=ticket_url,
            wss_url=wss_url,
            expires_at=expires_at,
            capabilities=capabilities,
        )

    async def validate_token(self, token: str) -> GatewayTokenClaim:
        """Verifica firma + expiración. Fail-closed."""
        claim = self._active_tokens.get(token)
        if claim is None:
            raise GatewayTokenInvalid("Token desconocido o ya revocado")
        now = datetime.now(tz=UTC)
        if now >= claim.expires_at:
            del self._active_tokens[token]
            raise GatewayTokenExpired(
                f"Token KasmVNC expirado a las {claim.expires_at.isoformat()}"
            )
        return claim

    async def revoke(self, *, workspace_id: UUID, tenant_id: UUID) -> None:
        """Revoca tokens del workspace. Idempotente."""
        to_remove = [
            t
            for t, c in self._active_tokens.items()
            if c.workspace_id == workspace_id and c.tenant_id == tenant_id
        ]
        for t in to_remove:
            del self._active_tokens[t]
        self._status[workspace_id] = KasmVncConnectionStatus.DISCONNECTED
        logger.info(
            "kasmvnc_gateway.revoked",
            extra={"workspace_id": str(workspace_id), "tokens_revoked": len(to_remove)},
        )

    def connection_status(self, workspace_id: UUID) -> KasmVncConnectionStatus:
        return self._status.get(workspace_id, KasmVncConnectionStatus.DISCONNECTED)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _mint_token(
        self,
        *,
        workspace_id: UUID,
        tenant_id: UUID,
        human_operator_id: UUID,
        nonce: str,
        expires_at: datetime,
    ) -> str:
        payload = json.dumps(
            {
                "workspace_id": str(workspace_id),
                "tenant_id": str(tenant_id),
                "human_operator_id": str(human_operator_id),
                "nonce": nonce,
                "exp": expires_at.isoformat(),
                "adapter": "kasmvnc",
            },
            sort_keys=True,
        ).encode()
        return hmac.new(self._cfg.signing_key, payload, hashlib.sha256).hexdigest()
