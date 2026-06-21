"""RemoteDesktopGatewayPort — contrato del gateway de escritorio remoto.

T075 — implementado en ``src/`` desde el contrato de spec 002.

Cubre: FR-001, FR-002, FR-003, FR-004, FR-005, FR-038, FR-044, FR-047, NFR-001.

Constitución IV: fail-closed — token inválido o expirado → sesión NO arranca.
Multi-tenant strict: validación incluye match (tenant_id, workspace_id).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID


class RemoteDesktopGatewayError(RuntimeError):
    """Base."""


class GatewayTokenInvalid(RemoteDesktopGatewayError):
    """Token efímero malformado, mal firmado o no pertenece al tenant."""


class GatewayTokenExpired(RemoteDesktopGatewayError):
    """Token caducado. El caller debe re-acuñar via WorkspaceLifecyclePort."""


class GatewayCapabilityUnavailable(RemoteDesktopGatewayError):
    """El stack actual del gateway no soporta la capability solicitada."""


class GatewayCapability(StrEnum):
    VIDEO = "video"
    AUDIO_INBOUND = "audio_inbound"
    AUDIO_OUTBOUND = "audio_outbound"
    CLIPBOARD_INBOUND = "clipboard_inbound"
    CLIPBOARD_OUTBOUND = "clipboard_outbound"
    FILE_UPLOAD = "file_upload"
    FILE_DOWNLOAD = "file_download"


@dataclass(frozen=True, slots=True)
class GatewayTokenClaim:
    """Claims verificados de un token efímero. No contiene material secreto."""

    workspace_id: UUID
    tenant_id: UUID
    human_operator_id: UUID
    issued_at: datetime
    expires_at: datetime
    capabilities: frozenset[GatewayCapability]


@dataclass(frozen=True, slots=True)
class GatewaySessionTicket:
    """Ticket que el panel pasa al cliente remoto del formador."""

    ticket_url: str
    wss_url: str
    expires_at: datetime
    capabilities: frozenset[GatewayCapability]
    issued_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@runtime_checkable
class RemoteDesktopGatewayPort(Protocol):
    """Puerto del gateway remoto.

    Implementaciones esperadas:
      - SelkiesGatewayAdapter (MVP default).
      - KasmVncGatewayAdapter (fallback).
      - InMemoryRemoteDesktopGateway (tests).
    """

    async def issue_session_ticket(
        self,
        *,
        workspace_id: UUID,
        tenant_id: UUID,
        human_operator_id: UUID,
        ttl_seconds: int,
        capabilities: frozenset[GatewayCapability],
    ) -> GatewaySessionTicket: ...

    async def validate_token(self, token: str) -> GatewayTokenClaim: ...

    async def revoke(
        self, *, workspace_id: UUID, tenant_id: UUID
    ) -> None: ...
