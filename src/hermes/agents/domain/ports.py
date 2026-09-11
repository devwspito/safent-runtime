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


class CannotUpdateDefaultAgent(ValueError):
    """El agente 'default' (CEO) no se puede editar vía API.

    El CEO tiene un system prompt world-class fijo; modificarlo
    podría degradar el comportamiento del SO. Si necesitas personalizar
    el CEO, usa el campo 'instructions' del agente default directamente
    en el daemon (no expuesto al canal REST por diseño).
    """


class CannotDeleteDefaultAgent(ValueError):
    """El agente 'default' del SO no se puede eliminar."""


class CannotDeleteLastAgent(ValueError):
    """Siempre debe quedar al menos un agente."""


class LicenseExceeded(RuntimeError):
    """Associate license: max_agents limit reached.

    Raised before any agent is created — no state is modified.
    Data is never deleted to enforce this limit (invariant).
    """


class LicenseExpired(RuntimeError):
    """Associate license: expires_at is in the past.

    Raised for create_agent and enqueue when in associate edition.
    Existing agents and their data are never deleted (invariant).
    """


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

    def persona_for(self, agent_id: str | None) -> PersonaSpec:
        """PersonaSpec efectiva para el agent_id (o el activo si es None).

        Ordinary missing profiles retain the default fallback. A retired factory
        ID raises FactoryAgentRetired, never silently executes as another agent.
        """
        ...
