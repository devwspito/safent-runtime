"""AgentCapabilityBinding aggregate (T014).

Extends the Capabilities bounded context with the dynamic assignment of
global capabilities (PlatformModels or Skills) to agent_ids.

Domain layer — pure Python, zero infra dependencies.

Invariants (data-model.md):
- Assigns a CapabilityRef (kind ∈ {platform, skill} + id + version) to an agent_id.
- agent_id must belong to the same tenant_id as the binding.
- bound_by is derived from sender_uid (D-Bus peer cred), NEVER from payload.
- Bind/unbind is idempotent.
- Does NOT reference integrations/credentials (access ≠ capability).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from hermes.platforms.domain.value_objects import CapabilityRef


class BindingState(StrEnum):
    BOUND = "bound"
    UNBOUND = "unbound"


class CapabilityAlreadyBound(RuntimeError):
    """Attempting to bind an already-bound capability (idempotent — not an error)."""


class IntegrationCapabilityForbidden(ValueError):
    """Integrations (StorageState/credentials) cannot be assigned via binding (FR-037)."""


@dataclass
class AgentCapabilityBinding:
    """Dynamic assignment of a global capability to an agent.

    Lifecycle: bound → unbound (revocation). Idempotent.
    """

    binding_id: str
    tenant_id: str
    agent_id: str
    capability: CapabilityRef
    bound_by: int  # UID derived from D-Bus sender_uid, NEVER from payload
    state: BindingState = BindingState.BOUND
    bound_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    unbound_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.binding_id:
            raise ValueError("AgentCapabilityBinding.binding_id cannot be empty")
        if not self.tenant_id:
            raise ValueError("AgentCapabilityBinding.tenant_id cannot be empty")
        if not self.agent_id:
            raise ValueError("AgentCapabilityBinding.agent_id cannot be empty")
        # Enforce no integrations — kind must be platform or skill (CapabilityRef validates)

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        agent_id: str,
        capability: CapabilityRef,
        bound_by: int,
    ) -> AgentCapabilityBinding:
        """Factory: create a new active binding with a generated id."""
        return cls(
            binding_id=uuid4().hex,
            tenant_id=tenant_id,
            agent_id=agent_id,
            capability=capability,
            bound_by=bound_by,
        )

    @property
    def is_active(self) -> bool:
        return self.state == BindingState.BOUND

    def unbind(self) -> AgentCapabilityBinding:
        """Transition bound → unbound (idempotent if already unbound)."""
        if self.state == BindingState.UNBOUND:
            return self
        import dataclasses  # noqa: PLC0415
        return dataclasses.replace(
            self,
            state=BindingState.UNBOUND,
            unbound_at=datetime.now(tz=UTC),
        )

    def to_dict(self) -> dict:
        """Serialize for D-Bus JSON transport (no PII, no credentials)."""
        return {
            "binding_id": self.binding_id,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "capability_kind": self.capability.kind,
            "capability_id": self.capability.capability_id,
            "capability_version": self.capability.version,
            "bound_by_uid": self.bound_by,
            "bound_at": self.bound_at.isoformat(),
            "state": str(self.state),
        }
