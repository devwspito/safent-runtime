"""TenantBindingService — binding del nodo a un tenant del operador.

Spec 003 FR-019, FR-020, FR-032. Un nodo tiene 0 o 1 TenantBinding
ACTIVE. Se puede revocar y rebindear, pero NUNCA dos ACTIVE a la vez
(invariante del unique partial index migration 014).

Modos:
  - CLOUD_SAAS_MANAGED: tenant emite los node_certs; binding incluye
    `tenant_cosign_identity_override` del operador.
  - SELF_HOSTED: tenant == operador local; binding trivial.

Estados:
  NEVER_BOUND → ACTIVE → REVOKED → ACTIVE (re-bind)
  ACTIVE → REBINDING → ACTIVE (rotación de tenant)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4


class BindingError(RuntimeError):
    pass


class ActiveBindingExistsError(BindingError):
    """Intento de crear ACTIVE cuando ya hay una."""


class BindingStateInvalid(BindingError):
    pass


class TenantBindingState(StrEnum):
    NEVER_BOUND = "never_bound"
    ACTIVE = "active"
    REVOKED = "revoked"
    REBINDING = "rebinding"


@dataclass(frozen=True, slots=True)
class TenantBinding:
    binding_id: UUID
    node_installation_id: UUID
    tenant_id: UUID | None
    state: TenantBindingState
    bound_at: datetime | None
    revoked_at: datetime | None
    last_rebound_at: datetime | None
    revocation_cause: str | None
    tenant_provided_endpoint: str | None
    tenant_cosign_identity_override: str | None


@dataclass(slots=True)
class TenantBindingService:
    _bindings_by_node: dict[UUID, TenantBinding] = field(
        default_factory=dict
    )

    def bind(
        self,
        *,
        node_installation_id: UUID,
        tenant_id: UUID,
        tenant_provided_endpoint: str | None = None,
        tenant_cosign_identity_override: str | None = None,
    ) -> TenantBinding:
        existing = self._bindings_by_node.get(node_installation_id)
        if existing is not None and existing.state == TenantBindingState.ACTIVE:
            raise ActiveBindingExistsError(
                f"node {node_installation_id} ya tiene binding ACTIVE"
            )
        now = datetime.now(tz=UTC)
        binding = TenantBinding(
            binding_id=uuid4(),
            node_installation_id=node_installation_id,
            tenant_id=tenant_id,
            state=TenantBindingState.ACTIVE,
            bound_at=now,
            revoked_at=None,
            last_rebound_at=None,
            revocation_cause=None,
            tenant_provided_endpoint=tenant_provided_endpoint,
            tenant_cosign_identity_override=(
                tenant_cosign_identity_override
            ),
        )
        self._bindings_by_node[node_installation_id] = binding
        return binding

    def revoke(
        self,
        *,
        node_installation_id: UUID,
        cause: str,
    ) -> TenantBinding:
        current = self._fetch(node_installation_id)
        if current.state == TenantBindingState.REVOKED:
            return current
        updated = replace(
            current,
            state=TenantBindingState.REVOKED,
            revoked_at=datetime.now(tz=UTC),
            revocation_cause=cause,
        )
        self._bindings_by_node[node_installation_id] = updated
        return updated

    def begin_rebind(
        self, *, node_installation_id: UUID
    ) -> TenantBinding:
        current = self._fetch(node_installation_id)
        if current.state not in (
            TenantBindingState.ACTIVE,
            TenantBindingState.REVOKED,
        ):
            raise BindingStateInvalid(
                f"begin_rebind requiere ACTIVE/REVOKED, está {current.state}"
            )
        updated = replace(current, state=TenantBindingState.REBINDING)
        self._bindings_by_node[node_installation_id] = updated
        return updated

    def complete_rebind(
        self,
        *,
        node_installation_id: UUID,
        new_tenant_id: UUID,
        new_endpoint: str | None = None,
        new_cosign_identity_override: str | None = None,
    ) -> TenantBinding:
        current = self._fetch(node_installation_id)
        if current.state != TenantBindingState.REBINDING:
            raise BindingStateInvalid(
                f"complete_rebind requiere REBINDING, está {current.state}"
            )
        now = datetime.now(tz=UTC)
        updated = replace(
            current,
            tenant_id=new_tenant_id,
            state=TenantBindingState.ACTIVE,
            bound_at=current.bound_at or now,
            last_rebound_at=now,
            tenant_provided_endpoint=new_endpoint,
            tenant_cosign_identity_override=new_cosign_identity_override,
        )
        self._bindings_by_node[node_installation_id] = updated
        return updated

    def get(self, *, node_installation_id: UUID) -> TenantBinding | None:
        return self._bindings_by_node.get(node_installation_id)

    def has_active_binding(self, *, node_installation_id: UUID) -> bool:
        b = self._bindings_by_node.get(node_installation_id)
        return b is not None and b.state == TenantBindingState.ACTIVE

    def _fetch(self, node_id: UUID) -> TenantBinding:
        if node_id not in self._bindings_by_node:
            raise BindingStateInvalid(
                f"no hay binding para node {node_id}"
            )
        return self._bindings_by_node[node_id]
