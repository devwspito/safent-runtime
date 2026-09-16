"""Child proposal transport: exact account binding, never approval/execution."""

import json
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from hermes.capabilities.tool_sensitivity import SensitivityCategory, sensitivity
from hermes.runtime.managed_ads_mcp import AdsScopedClient
from hermes.runtime.managed_ads_policy import ManagedAdsUnavailable, apply_signed_ads
from hermes.runtime.managed_ads_transport import ManagedAdsTransport
from tests.unit import test_managed_ads_policy as policy_fixture
from tests.unit.test_managed_ads_transport import signed_token

ads_policy = policy_fixture.ads_policy
pytestmark = pytest.mark.unit


def arguments(binding):
    return {
        "entity_ref": f"meta:ad_set:{binding.business_id}:{binding.connection_id}:456",
        "child_plan": {
            "schema_version": 1,
            "platform": "meta",
            "kind": "ad",
            "status": "PAUSED",
            "native": {"name": "Propuesta de anuncio", "creative_id": "789"},
        },
        "cause": {"text": "Revisión humana del anuncio propuesto"},
    }


@pytest.mark.asyncio
async def test_child_proposal_is_discoverable_spend_with_four_explicit_paused_plans(ads_policy):
    store, _, _, envelope = ads_policy
    apply_signed_ads(store, envelope())
    local = Mock()
    client = AdsScopedClient(store.db_path, local, lambda: store)
    await client.initialize()
    tools = {item["name"]: item for item in await client.list_tools()}
    schema = tools["propose_ad_child"]["inputSchema"]
    assert set(schema["required"]) == {"grant_id", "arguments"}
    assert set(schema["properties"]["arguments"]["required"]) == {
        "entity_ref",
        "child_plan",
        "cause",
    }
    plans = schema["properties"]["arguments"]["properties"]["child_plan"]["anyOf"]
    assert len(plans) == 4
    for plan in plans:
        definition = schema["$defs"][plan["$ref"].split("/")[-1]]
        assert definition["properties"]["status"]["const"] == "PAUSED"
        assert definition["additionalProperties"] is False
    assert SensitivityCategory.SPEND in sensitivity("mcp__safent-ads__propose_ad_child", {})
    assert not {"approve", "execute", "submit_approval", "attach_creative"} & set(tools)
    local.assert_not_called()


@pytest.mark.asyncio
async def test_child_snapshot_is_frozen_and_forwarded_once_under_signed_binding(ads_policy):
    store, key, binding, envelope = ads_policy
    apply_signed_ads(store, envelope())
    original = arguments(binding)
    expected = deepcopy(original)
    requests = []

    async def post(url, bearer, body, **_kwargs):
        requests.append((url, bearer, body))
        if url.endswith("/token"):
            original["child_plan"]["native"]["creative_id"] = "999"
            return {"grant_token": signed_token(key, binding)}
        return {"proposal_id": "fictional-pending-child"}

    result = await ManagedAdsTransport(store, post=post).call(
        binding.grant_id,
        "propose_ad_child",
        original,
        expected_binding=binding.model_dump(),
    )
    assert result == {"proposal_id": "fictional-pending-child"}
    assert len(requests) == 2
    assert requests[0][2] == {"grant_id": binding.grant_id}
    assert requests[1][0].endswith("/api/v1/managed/tools/propose_ad_child")
    assert requests[1][2] == expected
    assert "grant_token" not in json.dumps(result)
    assert "business_id" not in requests[1][2]


@pytest.mark.asyncio
async def test_stale_child_binding_is_denied_before_token_request(ads_policy):
    store, _, binding, envelope = ads_policy
    apply_signed_ads(store, envelope())
    post = AsyncMock()
    with pytest.raises(ManagedAdsUnavailable):
        await ManagedAdsTransport(store, post=post).call(
            binding.grant_id,
            "propose_ad_child",
            arguments(binding),
            expected_binding={**binding.model_dump(), "resource_revision": 2},
        )
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_child_http_router_preserves_binding_precondition_and_no_store(
    ads_policy, monkeypatch
):
    import httpx
    from fastapi import FastAPI

    from hermes.shell_server import ads_bridge

    store, key, binding, envelope = ads_policy
    apply_signed_ads(store, envelope())
    post = AsyncMock(
        side_effect=[
            {"grant_token": signed_token(key, binding)},
            {"proposal_id": "fictional-child"},
        ]
    )
    monkeypatch.setattr(
        ads_bridge,
        "ManagedAdsTransport",
        lambda value: ManagedAdsTransport(value, post=post),
    )
    app = FastAPI()
    app.include_router(ads_bridge.create_ads_bridge_router(store.db_path, store._vault))
    payload = {
        "grant_id": binding.grant_id,
        "arguments": arguments(binding),
        "expected_binding": binding.model_dump(),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="https://local.test",
    ) as client:
        denied = await client.post(
            "/api/v1/ads/managed/tools/propose_ad_child",
            json={
                **payload,
                "expected_binding": {**binding.model_dump(), "revision": 2},
            },
        )
        assert denied.status_code == 403
        post.assert_not_awaited()
        accepted = await client.post("/api/v1/ads/managed/tools/propose_ad_child", json=payload)
    assert accepted.status_code == 200
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.json() == {"proposal_id": "fictional-child"}
    assert post.call_args_list[1].args[2] == payload["arguments"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["bootstrap", "response"])
async def test_revoked_child_proposal_never_retries_or_discloses_late_result(ads_policy, phase):
    store, key, binding, envelope = ads_policy
    apply_signed_ads(store, envelope())
    requests = []

    async def post(url, _bearer, _body, **_kwargs):
        requests.append(url)
        if url.endswith("/token"):
            if phase == "bootstrap":
                apply_signed_ads(store, envelope(2, bindings=[]))
            return {"grant_token": signed_token(key, binding)}
        apply_signed_ads(store, envelope(2, bindings=[]))
        return {"proposal_id": "late-result-must-not-leak"}

    with pytest.raises(ManagedAdsUnavailable):
        await ManagedAdsTransport(store, post=post).call(
            binding.grant_id,
            "propose_ad_child",
            arguments(binding),
        )
    assert len(requests) == (1 if phase == "bootstrap" else 2)


@pytest.mark.asyncio
async def test_uncertain_child_proposal_has_no_automatic_replay(ads_policy):
    store, key, binding, envelope = ads_policy
    apply_signed_ads(store, envelope())
    post = AsyncMock(side_effect=[{"grant_token": signed_token(key, binding)}, TimeoutError()])
    with pytest.raises(TimeoutError):
        await ManagedAdsTransport(store, post=post).call(
            binding.grant_id,
            "propose_ad_child",
            arguments(binding),
        )
    assert post.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["approve", "execute", "submit_approval", "attach_creative"])
async def test_unsupported_authority_is_not_added_with_child_proposals(ads_policy, tool):
    store, _, binding, envelope = ads_policy
    apply_signed_ads(store, envelope())
    post = AsyncMock()
    with pytest.raises(ManagedAdsUnavailable):
        await ManagedAdsTransport(store, post=post).call(binding.grant_id, tool, {})
    post.assert_not_awaited()
