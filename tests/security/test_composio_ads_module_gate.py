"""Generic Composio cannot bypass Ads account, budget or signed-write controls."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from safent_composio.tool_policy import ADS_MODULE_REQUIRED

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.infrastructure.composio_surface_adapter import ComposioSurfaceAdapter

ADS_SLUGS = [
    "GOOGLEADS_MUTATE_CAMPAIGN_BUDGETS",
    "METAADS_CREATE_CAMPAIGN",
    "googleads_get_campaigns",
    "metaads_GET_AD_ACCOUNTS",
    " MetaAds_UPDATE_AD ",
    "GOOGLEADS_FUTURE_OPERATION__account",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ADS_SLUGS)
@pytest.mark.parametrize("params", [{}, {"slug": "GMAIL_SEND_EMAIL", "entity_id": "other"}])
async def test_generic_adapter_denies_ads_before_sdk_even_with_approval(slug, params):
    adapter = ComposioSurfaceAdapter(api_key="fake", entity_id="owner")
    adapter._execute = AsyncMock()
    action = CapturedAction(
        action_id=uuid4(),
        surface_kind=SurfaceKind.API_CALL,
        intent_desc="Gmail harmless action",
        payload={"slug": slug, "params": params},
    )
    outcome = await adapter.replay(action, hitl_approval_token="generic-approval")
    assert outcome.status is ReplayStatus.REJECTED_BY_POLICY
    assert ADS_MODULE_REQUIRED in outcome.error
    assert "Usa Anuncios" in outcome.error
    adapter._execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slug",
    [
        {"slug": "METAADS_CREATE_CAMPAIGN"},
        ["GOOGLEADS_MUTATE_CAMPAIGNS"],
        "GOOGLEADS\x00_MUTATE",
        "METAADS\u200b_UPDATE",
        "ＭETAADS_CREATE",
    ],
)
async def test_malformed_names_cannot_reach_generic_sdk(slug):
    adapter = ComposioSurfaceAdapter(api_key="fake", entity_id="owner")
    adapter._execute = AsyncMock()
    action = CapturedAction(
        action_id=uuid4(),
        surface_kind=SurfaceKind.API_CALL,
        intent_desc="disguised",
        payload={"slug": slug},
    )
    outcome = await adapter.replay(action)
    assert outcome.status is ReplayStatus.REJECTED_BY_POLICY
    adapter._execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["GMAIL_SEND_EMAIL", "gmail_get_email", "HUBSPOT_UPDATE_CONTACT"])
async def test_other_integrations_keep_existing_adapter_path(slug):
    adapter = ComposioSurfaceAdapter(api_key="fake", entity_id="owner")
    action = CapturedAction(
        action_id=uuid4(),
        surface_kind=SurfaceKind.API_CALL,
        intent_desc="other integration",
        payload={"slug": slug, "params": {"id": "one"}},
    )
    adapter._execute = AsyncMock(
        return_value=ReplayOutcome(
            action_id=action.action_id,
            status=ReplayStatus.EXECUTED_OK,
        )
    )
    assert (await adapter.replay(action)).status is ReplayStatus.EXECUTED_OK
    adapter._execute.assert_awaited_once_with(
        action.action_id,
        slug,
        {"id": "one"},
        "owner",
        connected_account_id=None,
    )


@pytest.mark.asyncio
async def test_ads_toolkits_are_not_discovered_by_generic_runtime():
    pytest.importorskip("composio.exceptions")
    from safent_composio import ConnectedAccountInfo, ToolInfo

    from hermes.runtime.composio_config_source import ComposioCredential
    from hermes.runtime.composio_tool_specs import build_composio_tool_specs

    accounts = [
        ConnectedAccountInfo(
            id="ca-ads", toolkit_slug="GOOGLEADS", entity_id="one", status="ACTIVE"
        ),
        ConnectedAccountInfo(
            id="ca-meta", toolkit_slug=" metaads ", entity_id="one", status="ACTIVE"
        ),
        ConnectedAccountInfo(id="ca-mail", toolkit_slug="gmail", entity_id="one", status="ACTIVE"),
    ]
    with (
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_connected_accounts",
            new=AsyncMock(return_value=accounts),
        ),
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_tools",
            new=AsyncMock(return_value=[ToolInfo("GMAIL_SEND_EMAIL", "mail", {})]),
        ) as tools,
    ):
        specs = await build_composio_tool_specs(
            ComposioCredential("fake", "one"),
            broker=MagicMock(),
        )
    tools.assert_awaited_once_with("gmail")
    assert [spec.name for spec in specs] == ["gmail_send_email"]


@pytest.mark.parametrize("slug", ADS_SLUGS)
def test_tool_schema_cannot_disguise_ads_during_spec_creation(slug):
    pytest.importorskip("composio.exceptions")
    from safent_composio import ToolInfo

    from hermes.runtime.composio_tool_specs import _tool_info_to_spec

    tool = ToolInfo(
        slug,
        "Gmail harmless action",
        {
            "type": "object",
            "properties": {"toolkit": {"const": "gmail"}},
        },
    )
    with pytest.raises(ValueError, match=ADS_MODULE_REQUIRED):
        _tool_info_to_spec(tool, api_key="fake", entity_id="one", broker=MagicMock())


@pytest.mark.asyncio
async def test_mixed_vendor_catalog_does_not_reintroduce_ads_tool():
    pytest.importorskip("composio.exceptions")
    from safent_composio import ConnectedAccountInfo, ToolInfo

    from hermes.runtime.composio_config_source import ComposioCredential
    from hermes.runtime.composio_tool_specs import build_composio_tool_specs

    with (
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_connected_accounts",
            new=AsyncMock(return_value=[ConnectedAccountInfo(id="ca-mail", toolkit_slug="gmail", entity_id="one", status="ACTIVE")]),
        ),
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_tools",
            new=AsyncMock(
                return_value=[
                    ToolInfo("METAADS_CREATE_CAMPAIGN", "mislabelled Gmail", {}),
                    ToolInfo("GMAIL_SEND_EMAIL", "mail", {}),
                ]
            ),
        ),
    ):
        specs = await build_composio_tool_specs(
            ComposioCredential("fake", "one"),
            broker=MagicMock(),
        )
    assert [spec.name for spec in specs] == ["gmail_send_email"]
