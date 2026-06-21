"""Puerto del registro de agentes — propiedad del daemon.

El registro es el ÚNICO escritor del estado de agentes. El control-plane D-Bus
invoca estas operaciones (autoría por sender_uid); el shell solo lee.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hermes.agents.domain.agent import Agent, AgentDraft
from hermes.prompts.persona import PersonaSpec


class AgentNotFound(LookupError):
    """No existe un agente con ese agent_id."""


class CannotDeleteDefaultAgent(ValueError):
    """El agente 'default' del SO no se puede eliminar."""


class CannotDeleteLastAgent(ValueError):
    """Siempre debe quedar al menos un agente."""


@runtime_checkable
class AgentRegistryPort(Protocol):
    """Estado nativo del daemon: roster de agentes + agente activo."""

    def list_agents(self) -> list[Agent]: ...

    def get_agent(self, agent_id: str) -> Agent:
        """Devuelve el agente o lanza AgentNotFound."""
        ...

    def create_agent(self, draft: AgentDraft) -> Agent: ...

    def update_agent(self, agent_id: str, draft: AgentDraft) -> Agent:
        """Actualiza campos editables. Lanza AgentNotFound si no existe."""
        ...

    def delete_agent(self, agent_id: str) -> None:
        """Elimina un agente. Lanza CannotDeleteDefaultAgent / CannotDeleteLastAgent."""
        ...

    def active_agent_id(self) -> str:
        """ID del agente activo (el que recibe el chat por defecto)."""
        ...

    def set_active_agent(self, agent_id: str) -> None:
        """Marca el agente activo. Lanza AgentNotFound si no existe."""
        ...

    def persona_for(self, agent_id: str | None) -> PersonaSpec:
        """PersonaSpec efectiva para el agent_id (o el activo si es None).

        Fail-soft: si el agent_id no existe, cae al agente activo; si tampoco,
        al primero. Nunca lanza — el daemon siempre puede razonar con ALGUNA
        persona.
        """
        ...
