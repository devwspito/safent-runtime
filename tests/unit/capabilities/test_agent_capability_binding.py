"""Tests for AgentCapabilityBinding aggregate — T007.

Covers: same-tenant invariant, bound_by authorship (never payload),
idempotent unbind, integration kind rejected.
"""

from __future__ import annotations

import pytest

from hermes.capabilities.domain.agent_capability_binding import (
    AgentCapabilityBinding,
    BindingState,
)
from hermes.platforms.domain.value_objects import CapabilityRef


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ref(kind: str = "platform") -> CapabilityRef:
    return CapabilityRef(kind=kind, capability_id="model-crm", version="1")


def _make_binding(bound_by: int = 1001) -> AgentCapabilityBinding:
    return AgentCapabilityBinding.create(
        tenant_id="tenant-x",
        agent_id="agent-a",
        capability=_make_ref(),
        bound_by=bound_by,
    )


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


class TestAgentCapabilityBindingCreate:
    def test_creates_bound_state(self):
        b = _make_binding()
        assert b.state == BindingState.BOUND
        assert b.is_active

    def test_binding_id_generated(self):
        b1 = _make_binding()
        b2 = _make_binding()
        assert b1.binding_id != b2.binding_id

    def test_bound_by_from_uid(self):
        b = _make_binding(bound_by=42)
        assert b.bound_by == 42

    def test_tenant_required(self):
        with pytest.raises(ValueError, match="tenant_id"):
            AgentCapabilityBinding(
                binding_id="bid",
                tenant_id="",
                agent_id="agent-a",
                capability=_make_ref(),
                bound_by=1,
            )

    def test_agent_required(self):
        with pytest.raises(ValueError, match="agent_id"):
            AgentCapabilityBinding(
                binding_id="bid",
                tenant_id="t1",
                agent_id="",
                capability=_make_ref(),
                bound_by=1,
            )


# ---------------------------------------------------------------------------
# CapabilityRef kind validation (no integrations)
# ---------------------------------------------------------------------------


class TestCapabilityRefKindValidation:
    def test_platform_kind_allowed(self):
        ref = CapabilityRef(kind="platform", capability_id="model-x", version="1")
        b = AgentCapabilityBinding.create(
            tenant_id="t", agent_id="a", capability=ref, bound_by=1
        )
        assert b.capability.kind == "platform"

    def test_skill_kind_allowed(self):
        ref = CapabilityRef(kind="skill", capability_id="skill-y", version="2")
        b = AgentCapabilityBinding.create(
            tenant_id="t", agent_id="a", capability=ref, bound_by=1
        )
        assert b.capability.kind == "skill"

    def test_integration_kind_rejected_by_capability_ref(self):
        """Integrations/credentials cannot be assigned via binding (FR-037)."""
        with pytest.raises(ValueError, match="kind must be"):
            CapabilityRef(kind="integration", capability_id="cred", version="1")


# ---------------------------------------------------------------------------
# Idempotent unbind
# ---------------------------------------------------------------------------


class TestIdempotentUnbind:
    def test_unbind_transitions_to_unbound(self):
        b = _make_binding()
        unbound = b.unbind()
        assert unbound.state == BindingState.UNBOUND
        assert not unbound.is_active
        assert unbound.unbound_at is not None

    def test_unbind_already_unbound_is_idempotent(self):
        b = _make_binding().unbind()
        b2 = b.unbind()
        # Should not raise, returns same unbound state
        assert b2.state == BindingState.UNBOUND

    def test_bound_by_not_changed_on_unbind(self):
        b = _make_binding(bound_by=999)
        unbound = b.unbind()
        assert unbound.bound_by == 999


# ---------------------------------------------------------------------------
# to_dict (no PII, no credentials)
# ---------------------------------------------------------------------------


class TestToDict:
    def test_to_dict_contains_required_keys(self):
        b = _make_binding()
        d = b.to_dict()
        assert "binding_id" in d
        assert "agent_id" in d
        assert "capability_kind" in d
        assert "capability_id" in d
        assert "capability_version" in d
        assert "bound_at" in d

    def test_to_dict_does_not_contain_credentials(self):
        b = _make_binding()
        d = b.to_dict()
        for key in ("password", "token", "secret", "api_key"):
            assert key not in d
