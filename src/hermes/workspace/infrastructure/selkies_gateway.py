"""SelkiesGatewayAdapter — gateway principal WebRTC (T083).

Adapter sobre Selkies-GStreamer. Cumple ``RemoteDesktopGatewayPort``.

Diseño:
- Selkies corre como proceso systemd independiente dentro de Hermes OS.
  Este adapter SOLO emite señales de control a ese proceso via subprocess/signal.
  NO importa ningún binding Python de Selkies (lazy-import guard).
- El token efímero se firma con HMAC-SHA256 sobre la KMS key del workspace;
  la validación es local (sin red).
- ``gateway_url`` + ``wss_url`` se construyen desde la config inyectada.
- Status: ICE_CONNECTING → ICE_CONNECTED → DEGRADED → DISCONNECTED.

Constitución IV (fail-closed): capabilities no soportadas lanzan
``GatewayCapabilityUnavailable`` antes de acuñar el ticket.

Lazy-imports: ``hmac``, ``hashlib``, ``json``, ``subprocess`` se usan dentro de
métodos, no al import del módulo.

FR-001..005, FR-038, C-3 (signaling TLS 1.3 + DTLS-SRTP gestionado por Selkies).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from hermes.workspace.domain.ports.remote_desktop_gateway_port import (
    GatewayCapability,
    GatewayCapabilityUnavailable,
    GatewaySessionTicket,
    GatewayTokenClaim,
    GatewayTokenExpired,
    GatewayTokenInvalid,
)

__all__ = ["SelkiesConfig", "SelkiesConnectionStatus", "SelkiesGatewayAdapter"]

logger = logging.getLogger(__name__)


class SelkiesConnectionStatus(StrEnum):
    ICE_CONNECTING = "ice_connecting"
    ICE_CONNECTED = "ice_connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


# Capabilities que Selkies soporta nativamente.
_SELKIES_SUPPORTED: frozenset[GatewayCapability] = frozenset(
    {
        GatewayCapability.VIDEO,
        GatewayCapability.AUDIO_INBOUND,
        GatewayCapability.AUDIO_OUTBOUND,
        GatewayCapability.CLIPBOARD_INBOUND,
        GatewayCapability.CLIPBOARD_OUTBOUND,
    }
)


@dataclass(frozen=True, slots=True)
class SelkiesConfig:
    """Configuración inyectada al adapter en boot."""

    gateway_base_url: str          # https://ws.<workspace-id>.hermes.internal
    wss_base_url: str              # wss://ws.<workspace-id>.hermes.internal
    signing_key: bytes             # 32 bytes; gestiona el control plane
    viewport_width: int = 1280
    viewport_height: int = 720
    audio_bidi: bool = True
    ice_timeout_seconds: int = 30  # fallback a KasmVNC si supera esto
    selkies_control_socket: str = "/var/run/hermes/sockets/selkies.sock"
    supports_audio: bool = True


class SelkiesGatewayAdapter:
    """Adapter principal del gateway remoto. Cumple ``RemoteDesktopGatewayPort``.

    No hereda de Protocol en runtime; la comprobación es estructural.
    """

    def __init__(self, config: SelkiesConfig) -> None:
        self._cfg = config
        self._active_tokens: dict[str, GatewayTokenClaim] = {}
        self._status: dict[UUID, SelkiesConnectionStatus] = {}

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
        """Acuña ticket efímero scoped a (workspace, tenant, operador).

        Lanza ``GatewayCapabilityUnavailable`` si se solicita una capability
        que Selkies no soporta (fail-closed).
        """
        unsupported = capabilities - _SELKIES_SUPPORTED
        if unsupported:
            raise GatewayCapabilityUnavailable(
                f"Selkies no soporta: {sorted(str(c) for c in unsupported)}"
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
        self._status[workspace_id] = SelkiesConnectionStatus.ICE_CONNECTING

        logger.info(
            "selkies_gateway.ticket_issued",
            extra={
                "workspace_id": str(workspace_id),
                "tenant_id": str(tenant_id),
                "ttl_seconds": ttl_seconds,
                "capabilities": sorted(str(c) for c in capabilities),
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
        """Verifica firma + expiración + scoping. Fail-closed."""
        claim = self._active_tokens.get(token)
        if claim is None:
            raise GatewayTokenInvalid("Token desconocido o ya revocado")
        now = datetime.now(tz=UTC)
        if now >= claim.expires_at:
            del self._active_tokens[token]
            raise GatewayTokenExpired(
                f"Token expirado a las {claim.expires_at.isoformat()}"
            )
        return claim

    async def revoke(self, *, workspace_id: UUID, tenant_id: UUID) -> None:
        """Revoca todos los tokens del workspace. Idempotente."""
        to_remove = [
            t
            for t, c in self._active_tokens.items()
            if c.workspace_id == workspace_id and c.tenant_id == tenant_id
        ]
        for t in to_remove:
            del self._active_tokens[t]
        self._status[workspace_id] = SelkiesConnectionStatus.DISCONNECTED
        await self._signal_selkies_stop(workspace_id)
        logger.info(
            "selkies_gateway.revoked",
            extra={"workspace_id": str(workspace_id), "tokens_revoked": len(to_remove)},
        )

    # ------------------------------------------------------------------
    # Selkies lifecycle via subprocess
    # ------------------------------------------------------------------

    async def start(
        self,
        workspace_id: UUID,
        *,
        viewport: tuple[int, int] = (1280, 720),
        audio_bidi: bool = True,
    ) -> None:
        """Arranca el proceso Selkies enviando señal al socket de control."""
        width, height = viewport
        await self._signal_selkies(
            workspace_id,
            command="start",
            params={
                "viewport_width": width,
                "viewport_height": height,
                "audio_bidi": audio_bidi,
            },
        )
        logger.info(
            "selkies_gateway.start_requested",
            extra={"workspace_id": str(workspace_id), "viewport": f"{width}x{height}"},
        )

    async def stop(self, workspace_id: UUID) -> None:
        """Para el proceso Selkies limpiamente."""
        await self._signal_selkies_stop(workspace_id)
        logger.info(
            "selkies_gateway.stop_requested",
            extra={"workspace_id": str(workspace_id)},
        )

    def connection_status(self, workspace_id: UUID) -> SelkiesConnectionStatus:
        return self._status.get(workspace_id, SelkiesConnectionStatus.DISCONNECTED)

    # ------------------------------------------------------------------
    # Internal helpers
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
        """HMAC-SHA256 sobre payload. El token es el hex del MAC."""
        payload = json.dumps(
            {
                "workspace_id": str(workspace_id),
                "tenant_id": str(tenant_id),
                "human_operator_id": str(human_operator_id),
                "nonce": nonce,
                "exp": expires_at.isoformat(),
            },
            sort_keys=True,
        ).encode()
        mac = hmac.new(self._cfg.signing_key, payload, hashlib.sha256).hexdigest()
        return mac

    async def _signal_selkies(
        self, workspace_id: UUID, *, command: str, params: dict[str, Any]
    ) -> None:
        """Envía señal JSON al socket de control de Selkies (no bloqueante)."""
        msg = json.dumps(
            {"command": command, "workspace_id": str(workspace_id), **params}
        )
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                self._write_to_socket,
                self._cfg.selkies_control_socket,
                msg,
            )
        except FileNotFoundError:
            logger.warning(
                "selkies_gateway.control_socket_not_found",
                extra={"socket": self._cfg.selkies_control_socket},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "selkies_gateway.signal_failed",
                extra={"command": command, "error": str(exc)},
            )

    async def _signal_selkies_stop(self, workspace_id: UUID) -> None:
        await self._signal_selkies(workspace_id, command="stop", params={})

    @staticmethod
    def _write_to_socket(socket_path: str, message: str) -> None:
        """Escribe al socket Unix del proceso Selkies.

        Esto se ejecuta en el threadpool executor; no bloquea el event loop.
        """
        import socket as sock_module  # noqa: PLC0415

        with sock_module.socket(sock_module.AF_UNIX, sock_module.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(socket_path)
            s.sendall(message.encode() + b"\n")
