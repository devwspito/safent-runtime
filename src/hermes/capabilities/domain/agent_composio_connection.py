"""AgentComposioConnection aggregate — binding de una cuenta Composio a un agente.

Dominio puro (sin infra). Modela el vínculo agente↔cuenta-Composio con:
  - connected_account_id: ID de la cuenta en Composio Cloud (fuente de verdad).
  - toolkit_slug: desnormalizado para filtrado runtime sin llamadas de red.
  - bound_by: UID del sender D-Bus; NUNCA del payload (CWE-862).

Razón del modelo dedicado (FR-037):
  AgentCapabilityBinding modela capabilities (skills/platform), con invariante
  explícito IntegrationCapabilityForbidden: "access ≠ capability". Las conexiones
  Composio son credenciales de acceso (tokens en Composio Cloud), no capabilities.
  Un modelo dedicado es aditivo, no toca ese invariante.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class BindingState(StrEnum):
    BOUND = "bound"
    UNBOUND = "unbound"


class ComposioConnectionAlreadyBound(RuntimeError):
    """Idempotente — no es un error, usada para devolver el binding existente."""


@dataclass
class AgentComposioConnection:
    """Asignación de una cuenta Composio conectada a un agente específico.

    Ciclo de vida: bound → unbound (revocación). Idempotente en bind.
    """

    binding_id: str
    tenant_id: str
    agent_id: str
    connected_account_id: str   # ID en Composio Cloud
    toolkit_slug: str           # desnormalizado para filtrado sin red
    bound_by: int               # sender_uid del bus, NUNCA del payload
    state: BindingState = BindingState.BOUND
    bound_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    unbound_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.binding_id:
            raise ValueError("AgentComposioConnection.binding_id no puede ser vacío")
        if not self.tenant_id:
            raise ValueError("AgentComposioConnection.tenant_id no puede ser vacío")
        if not self.agent_id:
            raise ValueError("AgentComposioConnection.agent_id no puede ser vacío")
        if not self.connected_account_id:
            raise ValueError("AgentComposioConnection.connected_account_id no puede ser vacío")
        if not self.toolkit_slug:
            raise ValueError("AgentComposioConnection.toolkit_slug no puede ser vacío")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        agent_id: str,
        connected_account_id: str,
        toolkit_slug: str,
        bound_by: int,
    ) -> AgentComposioConnection:
        """Factory: crea un nuevo binding activo con ID generado."""
        return cls(
            binding_id=uuid4().hex,
            tenant_id=tenant_id,
            agent_id=agent_id,
            connected_account_id=connected_account_id,
            toolkit_slug=toolkit_slug.lower(),
            bound_by=bound_by,
        )

    @property
    def is_active(self) -> bool:
        return self.state == BindingState.BOUND

    def unbind(self) -> AgentComposioConnection:
        """Transición bound → unbound (idempotente si ya está unbound)."""
        if self.state == BindingState.UNBOUND:
            return self
        return dataclasses.replace(
            self,
            state=BindingState.UNBOUND,
            unbound_at=datetime.now(tz=UTC),
        )

    def to_dict(self) -> dict:
        """Serialización para transporte D-Bus JSON (sin credenciales)."""
        return {
            "binding_id": self.binding_id,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "connected_account_id": self.connected_account_id,
            "toolkit_slug": self.toolkit_slug,
            "bound_by_uid": self.bound_by,
            "bound_at": self.bound_at.isoformat(),
            "state": str(self.state),
        }
