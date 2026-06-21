"""Tests for platform + capability D-Bus wiring (T021/T025).

Uses FakeDbusInterface (no real bus) and InMemoryPlatformModelRegistry.

Verifies:
- Mutators are fail-closed (unauthorized UID raises DbusAuthorizationError).
- Reads require no authZ.
- Authorship (bound_by) is NEVER taken from payload — always from sender_uid.
- bind/unbind/list_capabilities work end-to-end.
"""

from __future__ import annotations

import pytest

from tests.unit.platforms.fakes import (
    InMemoryCapabilityBindingRepo,
    InMemoryPlatformModelRegistry,
)
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.platforms.domain.platform_model import (
    PlatformArea,
    PlatformModel,
    Zone,
)
from hermes.platforms.domain.value_objects import (
    DomainName,
    LifecycleState,
    ModelVersion,
    NavigationPath,
    PlatformModelId,
    TourOrigin,
    ZoneHash,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_AUTHORIZED_UID = 1000
_UNAUTHORIZED_UID = 9999
_TENANT_ID = "tenant-test"


class _FakeAgentState:
    async def pause(self, by, reason): ...
    async def resume(self, by): ...


class _FakeApprovalGate:
    async def approve(self, proposal_id, approved_by): return "token"
    async def reject(self, proposal_id, rejected_by, reason): ...


@pytest.fixture
def platform_registry():
    return InMemoryPlatformModelRegistry()


@pytest.fixture
def binding_repo():
    return InMemoryCapabilityBindingRepo()


@pytest.fixture
def wiring(platform_registry, binding_repo):
    return DbusRuntimeServiceWiring(
        agent_state=_FakeAgentState(),
        approval_gate=_FakeApprovalGate(),
        authorized_uids=frozenset({_AUTHORIZED_UID}),
        platform_model_registry=platform_registry,
        capability_binding_repo=binding_repo,
        tenant_id=_TENANT_ID,
    )


def _make_model(model_id: str = "m1", state: LifecycleState = LifecycleState.APRENDIDA) -> PlatformModel:
    zone = Zone(
        zone_id="z1",
        zone_hash=ZoneHash.compute({"zone_id": "z1"}),
        member_refs=("a1",),
    )
    area = PlatformArea(
        area_id="a1",
        navigation_path=NavigationPath("/clientes"),
        zone_id="z1",
        domain_name=DomainName("Clientes"),
    )
    return PlatformModel(
        platform_model_id=PlatformModelId(model_id),
        version=ModelVersion(1),
        tenant_id=_TENANT_ID,
        site_ref="site-crm",
        lifecycle_state=state,
        origin=TourOrigin.GUIDED,
        areas=(area,),
        entities=(),
        landmarks=(),
        house_rules=(),
        zones=(zone,),
        staleness_marks=(),
        signature=None,
    )


# ---------------------------------------------------------------------------
# Reads require no authZ
# ---------------------------------------------------------------------------


class TestReadOperationsNoAuthZ:
    def test_list_platform_models_no_authz(self, wiring, platform_registry):
        platform_registry.save(_make_model())
        result = wiring.list_platform_models(_TENANT_ID)
        assert isinstance(result, list)
        assert len(result) == 1

    def test_get_platform_model_summary_no_authz(self, wiring, platform_registry):
        platform_registry.save(_make_model())
        summary = wiring.get_platform_model_summary("m1", _TENANT_ID)
        assert summary["model_id"] == "m1"

    def test_list_agent_capabilities_no_authz(self, wiring):
        result = wiring.list_agent_capabilities("agent-a", _TENANT_ID)
        assert result == []


# ---------------------------------------------------------------------------
# Mutators: fail-closed on unauthorized UID
# ---------------------------------------------------------------------------


class TestMutatorAuthzFailClosed:
    @pytest.mark.asyncio
    async def test_enable_platform_model_unauthorized_raises(self, wiring, platform_registry):
        platform_registry.save(_make_model())
        with pytest.raises(DbusAuthorizationError):
            await wiring.enable_platform_model(
                model_id="m1", tenant_id=_TENANT_ID, sender_uid=_UNAUTHORIZED_UID
            )

    @pytest.mark.asyncio
    async def test_disable_platform_model_unauthorized_raises(self, wiring, platform_registry):
        platform_registry.save(_make_model(state=LifecycleState.HABILITADA))
        with pytest.raises(DbusAuthorizationError):
            await wiring.disable_platform_model(
                model_id="m1", tenant_id=_TENANT_ID, sender_uid=_UNAUTHORIZED_UID
            )

    @pytest.mark.asyncio
    async def test_deprecate_platform_model_unauthorized_raises(self, wiring, platform_registry):
        platform_registry.save(_make_model())
        with pytest.raises(DbusAuthorizationError):
            await wiring.deprecate_platform_model(
                model_id="m1", tenant_id=_TENANT_ID, sender_uid=_UNAUTHORIZED_UID
            )

    @pytest.mark.asyncio
    async def test_bind_capability_unauthorized_raises(self, wiring):
        with pytest.raises(DbusAuthorizationError):
            await wiring.bind_capability_to_agent(
                agent_id="a",
                capability_kind="platform",
                capability_id="m1",
                capability_version="1",
                tenant_id=_TENANT_ID,
                sender_uid=_UNAUTHORIZED_UID,
            )

    @pytest.mark.asyncio
    async def test_unbind_capability_unauthorized_raises(self, wiring):
        with pytest.raises(DbusAuthorizationError):
            await wiring.unbind_capability_from_agent(
                agent_id="a",
                capability_kind="platform",
                capability_id="m1",
                tenant_id=_TENANT_ID,
                sender_uid=_UNAUTHORIZED_UID,
            )

    @pytest.mark.asyncio
    async def test_set_house_rule_unauthorized_raises(self, wiring):
        with pytest.raises(DbusAuthorizationError):
            await wiring.set_agent_house_rule(
                agent_id="a",
                model_id="m1",
                rule={"kind": "never_touch", "target_area_ref": "a1", "phrasing": "Nunca"},
                tenant_id=_TENANT_ID,
                sender_uid=_UNAUTHORIZED_UID,
            )


# ---------------------------------------------------------------------------
# Authorship never from payload
# ---------------------------------------------------------------------------


class TestAuthorshipFromSenderUid:
    @pytest.mark.asyncio
    async def test_bound_by_equals_sender_uid_not_payload(self, wiring, binding_repo):
        """bound_by must be sender_uid (from bus), never from any payload field."""
        result = await wiring.bind_capability_to_agent(
            agent_id="agent-a",
            capability_kind="platform",
            capability_id="model-crm",
            capability_version="1",
            tenant_id=_TENANT_ID,
            sender_uid=_AUTHORIZED_UID,
        )
        binding_id = result["binding_id"]
        binding = binding_repo.get(binding_id)
        assert binding.bound_by == _AUTHORIZED_UID
        # No "by" / "operator" / "uid" field should exist in the payload dict
        # (the binding was created purely from sender_uid)
        assert "operator" not in result
        assert "requested_by" not in result


# ---------------------------------------------------------------------------
# Bind/unbind end-to-end (fully functional per T026)
# ---------------------------------------------------------------------------


class TestBindUnbindEndToEnd:
    @pytest.mark.asyncio
    async def test_bind_and_list(self, wiring, binding_repo):
        await wiring.bind_capability_to_agent(
            agent_id="agent-a",
            capability_kind="platform",
            capability_id="model-crm",
            capability_version="1",
            tenant_id=_TENANT_ID,
            sender_uid=_AUTHORIZED_UID,
        )
        caps = wiring.list_agent_capabilities("agent-a", _TENANT_ID)
        assert len(caps) == 1
        assert caps[0]["capability_id"] == "model-crm"

    @pytest.mark.asyncio
    async def test_bind_idempotent(self, wiring, binding_repo):
        for _ in range(3):
            await wiring.bind_capability_to_agent(
                agent_id="agent-a",
                capability_kind="platform",
                capability_id="model-crm",
                capability_version="1",
                tenant_id=_TENANT_ID,
                sender_uid=_AUTHORIZED_UID,
            )
        caps = wiring.list_agent_capabilities("agent-a", _TENANT_ID)
        assert len(caps) == 1

    @pytest.mark.asyncio
    async def test_unbind_removes_capability(self, wiring, binding_repo):
        await wiring.bind_capability_to_agent(
            agent_id="agent-a",
            capability_kind="platform",
            capability_id="model-crm",
            capability_version="1",
            tenant_id=_TENANT_ID,
            sender_uid=_AUTHORIZED_UID,
        )
        await wiring.unbind_capability_from_agent(
            agent_id="agent-a",
            capability_kind="platform",
            capability_id="model-crm",
            tenant_id=_TENANT_ID,
            sender_uid=_AUTHORIZED_UID,
        )
        caps = wiring.list_agent_capabilities("agent-a", _TENANT_ID)
        assert caps == []

    @pytest.mark.asyncio
    async def test_unbind_idempotent_when_not_bound(self, wiring):
        result = await wiring.unbind_capability_from_agent(
            agent_id="agent-a",
            capability_kind="platform",
            capability_id="not-bound",
            tenant_id=_TENANT_ID,
            sender_uid=_AUTHORIZED_UID,
        )
        assert result is False  # no binding existed


# ---------------------------------------------------------------------------
# Enable/disable lifecycle end-to-end
# ---------------------------------------------------------------------------


class TestPlatformModelLifecycleMutators:
    @pytest.mark.asyncio
    async def test_enable_model(self, wiring, platform_registry):
        model = _make_model(state=LifecycleState.APRENDIDA)
        platform_registry.save(model)
        result = await wiring.enable_platform_model(
            model_id="m1", tenant_id=_TENANT_ID, sender_uid=_AUTHORIZED_UID
        )
        assert result is True
        loaded = platform_registry.get("m1", _TENANT_ID)
        assert loaded.lifecycle_state == LifecycleState.HABILITADA

    @pytest.mark.asyncio
    async def test_deprecate_model(self, wiring, platform_registry):
        model = _make_model(state=LifecycleState.APRENDIDA)
        platform_registry.save(model)
        result = await wiring.deprecate_platform_model(
            model_id="m1", tenant_id=_TENANT_ID, sender_uid=_AUTHORIZED_UID
        )
        assert result is True
        loaded = platform_registry.get("m1", _TENANT_ID)
        assert loaded.lifecycle_state == LifecycleState.DEPRECADA
