"""InMemoryRemoteDesktopGateway — fake de RemoteDesktopGatewayPort para tests.

Constitución V: tests base corren sin VM, sin Selkies, sin red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from hermes.workspace.domain.ports.remote_desktop_gateway_port import (
    GatewayCapability,
    GatewayCapabilityUnavailable,
    GatewaySessionTicket,
    GatewayTokenClaim,
    GatewayTokenExpired,
    GatewayTokenInvalid,
)

__all__ = ["InMemoryRemoteDesktopGateway"]


class InMemoryRemoteDesktopGateway:
    """Fake de RemoteDesktopGatewayPort. Gestiona tokens en memoria."""

    _ALL_CAPABILITIES: frozenset[GatewayCapability] = frozenset(GatewayCapability)

    def __init__(
        self,
        *,
        supported_capabilities: frozenset[GatewayCapability] | None = None,
    ) -> None:
        self._supported = supported_capabilities or self._ALL_CAPABILITIES
        self._active_tokens: dict[str, GatewayTokenClaim] = {}
        self.issued_tickets: list[GatewaySessionTicket] = []
        self.revoked_workspaces: list[UUID] = []

    async def issue_session_ticket(
        self,
        *,
        workspace_id: UUID,
        tenant_id: UUID,
        human_operator_id: UUID,
        ttl_seconds: int,
        capabilities: frozenset[GatewayCapability],
    ) -> GatewaySessionTicket:
        unsupported = capabilities - self._supported
        if unsupported:
            raise GatewayCapabilityUnavailable(
                f"InMemoryGateway no soporta: {sorted(str(c) for c in unsupported)}"
            )
        now = datetime.now(tz=UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)
        token = str(uuid4())
        claim = GatewayTokenClaim(
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
            issued_at=now,
            expires_at=expires_at,
            capabilities=capabilities,
        )
        self._active_tokens[token] = claim
        ticket = GatewaySessionTicket(
            ticket_url=f"http://in-memory-gateway/{workspace_id}?token={token}",
            wss_url=f"ws://in-memory-gateway/{workspace_id}/ws",
            expires_at=expires_at,
            capabilities=capabilities,
        )
        self.issued_tickets.append(ticket)
        return ticket

    async def validate_token(self, token: str) -> GatewayTokenClaim:
        claim = self._active_tokens.get(token)
        if claim is None:
            raise GatewayTokenInvalid("Token desconocido")
        if datetime.now(tz=UTC) >= claim.expires_at:
            del self._active_tokens[token]
            raise GatewayTokenExpired("Token expirado")
        return claim

    async def revoke(self, *, workspace_id: UUID, tenant_id: UUID) -> None:
        to_remove = [
            t
            for t, c in self._active_tokens.items()
            if c.workspace_id == workspace_id and c.tenant_id == tenant_id
        ]
        for t in to_remove:
            del self._active_tokens[t]
        self.revoked_workspaces.append(workspace_id)
