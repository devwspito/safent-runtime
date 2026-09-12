import base64
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest

from hermes.runtime.managed_ads_mcp import AdsScopedClient, scoped_ads_factory
from hermes.runtime.managed_ads_policy import ManagedAdsUnavailable, apply_signed_ads
from hermes.runtime.managed_ads_transport import ManagedAdsTransport
from tests.unit.test_managed_ads_policy import ads_policy as policy_fixture

ads_policy = policy_fixture

pytestmark = pytest.mark.unit


def signed_token(key, binding, *, nonce="nonce", **changes):
    now = int(time.time())
    claims = {
        **binding.model_dump(),
        "v": 1,
        "purpose": "ads_account_delegation",
        "aud": "safent-ads-central",
        "iat": now,
        "exp": now + 120,
        "jti": nonce,
        **changes,
    }
    raw = json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + key.sign(raw).hex()


@pytest.mark.asyncio
async def test_exact_token_per_request_never_cached_or_returned(ads_policy):
    store, key, binding, envelope = ads_policy
    apply_signed_ads(store, envelope())
    requests = []

    async def post(url, bearer, body, **_kwargs):
        requests.append((url, bearer, body))
        if url.endswith("/token"):
            assert body == {"grant_id": binding.grant_id}
            return {"grant_token": signed_token(key, binding, nonce=str(len(requests)))}
        return {"ok": True}

    transport = ManagedAdsTransport(store, post=post)
    for _ in range(2):
        assert await transport.call(binding.grant_id, "list_campaigns", {}) == {"ok": True}
    assert len(requests) == 4 and requests[1][1] != requests[3][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"revision": 2},
        {"instance_id": "other"},
        {"external_account_id": "act_123"},
        {"aud": "other"},
        {"exp": 1},
    ],
)
async def test_changed_bootstrap_claims_deny_before_central(ads_policy, changes):
    store, key, binding, envelope = ads_policy
    apply_signed_ads(store, envelope())
    post = AsyncMock(return_value={"grant_token": signed_token(key, binding, **changes)})
    with pytest.raises(ManagedAdsUnavailable):
        await ManagedAdsTransport(store, post=post).call(binding.grant_id, "list_campaigns", {})
    assert post.await_count == 1


@pytest.mark.asyncio
async def test_revoke_during_bootstrap_and_missing_grant_never_fall_back(ads_policy):
    store, key, binding, envelope = ads_policy
    apply_signed_ads(store, envelope())

    async def post(*_args, **_kwargs):
        apply_signed_ads(store, envelope(2, bindings=[]))
        return {"grant_token": signed_token(key, binding)}

    transport = ManagedAdsTransport(store, post=post)
    with pytest.raises(ManagedAdsUnavailable):
        await transport.call(binding.grant_id, "list_campaigns", {})
    with pytest.raises(ManagedAdsUnavailable):
        await transport.call(binding.grant_id, "list_campaigns", {})


@pytest.mark.asyncio
async def test_old_local_client_cannot_survive_managed_transition(ads_policy):
    store, _, _, envelope = ads_policy
    apply_signed_ads(store, envelope(mode="free"))
    local = AsyncMock()
    client = AdsScopedClient(store.db_path, lambda: local, lambda: store)
    await client.initialize()
    apply_signed_ads(store, envelope(2))
    with pytest.raises(ManagedAdsUnavailable):
        await client.call_tool("list_campaigns", {})
    local.call_tool.assert_not_awaited()
    await client.close()


@pytest.mark.asyncio
async def test_managed_catalog_never_initializes_local_and_other_slug_unchanged(ads_policy):
    store, _, _, envelope = ads_policy
    apply_signed_ads(store, envelope())
    factory = Mock()
    choose = scoped_ads_factory(store.db_path, factory)
    client = choose("safent-ads", object())
    await client.initialize()
    names = {tool["name"] for tool in await client.list_tools()}
    assert "get_approval_review" in names and "execute" not in names
    factory.assert_not_called()
    other = object()
    choose("unrelated", other)
    factory.assert_called_once_with(other)


@pytest.mark.asyncio
async def test_real_manager_reconnect_ignores_persisted_local_credentials(ads_policy, monkeypatch):
    from hermes.agents_os.infrastructure import dbus_runtime_service as wiring
    from hermes.mcp.application.mcp_server_manager import McpServerManager

    store, _, _, envelope = ads_policy
    apply_signed_ads(store, envelope())
    monkeypatch.setenv("HERMES_SHELL_DB", str(store.db_path))
    local_factory = Mock()
    manager = McpServerManager(
        client_factory=local_factory,
        scoped_client_factory=scoped_ads_factory(store.db_path, local_factory),
    )
    monkeypatch.setattr(wiring, "_import_seed_mcp_servers", Mock())
    monkeypatch.setattr(wiring, "_import_seed_companion_servers", Mock())
    monkeypatch.setattr(
        wiring,
        "_neus_load_entries",
        lambda: [
            {
                "server_id": "safent-ads",
                "argv": ["npx", "local"],
                "env": {"ADS_BEARER": "old-local"},
            }
        ],
    )
    await wiring.reconnect_persisted_mcp_servers(manager)
    server = manager._servers["safent-ads"]
    schema = server.get_tool("propose_pause").input_schema
    assert set(schema["required"]) == {"grant_id", "arguments"}
    assert "cause" in schema["properties"]["arguments"]["properties"]
    local_factory.assert_not_called()
    with pytest.raises(PermissionError):
        wiring._autowire_companion_env("safent-ads", {"ADS_BEARER": ""})


@pytest.mark.asyncio
async def test_managed_bridge_never_mints_or_proxies_local_session(ads_policy):
    import httpx
    from fastapi import FastAPI

    from hermes.shell_server.ads_bridge import create_ads_bridge_router

    store, _, _, envelope = ads_policy
    apply_signed_ads(store, envelope())
    app = FastAPI()
    app.state.dbus_proxy = AsyncMock()
    app.include_router(create_ads_bridge_router(store.db_path, store._vault))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://local.test"
    ) as client:
        for path in ["/api/v1/ads/bridge/session", "/ads/api/v1/auth/exchange", "/ads"]:
            response = await client.post(path)
            assert response.status_code == 403
        available = await client.get("/api/v1/ads/managed")
        assert available.json()["policy"]["mode"] == "managed"
    app.state.dbus_proxy.call_dict.assert_not_awaited()
