"""Ads managed/custom OAuth and tenant scoping; SDK doubles, no provider calls."""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

pytest.importorskip("composio.exceptions")
from hermes.integrations.composio.composio_client import ComposioApiError, ComposioClient


def sdk():
    client = MagicMock()
    client.auth_configs.get.return_value = NS(
        id="ac-owned",
        toolkit=NS(slug="metaads"),
        status="ENABLED",
        auth_scheme="OAUTH2",
        credentials={"client_secret": "NEVER-RETURN-THIS"},
    )
    client.auth_configs.list.return_value = NS(items=[])
    client.auth_configs.create.return_value = NS(id="ac-managed")
    client.connected_accounts.link.return_value = NS(
        id="ca-new", redirect_url="https://connect.example", status="INITIATED"
    )
    client.toolkits.get.return_value = NS(
        slug="googleads", enabled=True, composio_managed_auth_schemes=["OAUTH2"]
    )
    return client


@pytest.mark.asyncio
async def test_custom_meta_uses_selected_config_without_creating_managed():
    fake = sdk()
    client = ComposioClient("test", sdk=fake, auth_config_ids={"metaads": "ac-owned"})
    result = await client.initiate_connection(toolkit_slug="metaads", entity_id="safent-one")
    assert result.connected_account_id == "ca-new"
    fake.connected_accounts.link.assert_called_once_with(
        "safent-one", "ac-owned", callback_url=None, allow_multiple=True
    )
    fake.auth_configs.create.assert_not_called()
    assert "NEVER-RETURN-THIS" not in repr(await client.validate_auth_config("metaads", "ac-owned"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"status": "DISABLED"},
        {"status": None},
        {"toolkit": NS(slug="gmail")},
        {"auth_scheme": "API_KEY"},
        {"auth_scheme": None},
        {"id": "someone-else"},
    ],
)
async def test_invalid_custom_config_is_rejected_before_link(change):
    fake = sdk()
    fake.auth_configs.get.return_value.__dict__.update(change)
    client = ComposioClient("test", sdk=fake, auth_config_ids={"metaads": "ac-owned"})
    with pytest.raises(ComposioApiError):
        await client.initiate_connection(toolkit_slug="metaads", entity_id="safent-one")
    fake.connected_accounts.link.assert_not_called()
    fake.auth_configs.create.assert_not_called()


@pytest.mark.asyncio
async def test_selected_config_is_revalidated_after_disable():
    fake = sdk()
    client = ComposioClient("test", sdk=fake, auth_config_ids={"metaads": "ac-owned"})
    await client.validate_auth_config("metaads", "ac-owned")
    fake.auth_configs.get.return_value.status = "DISABLED"
    with pytest.raises(ComposioApiError):
        await client.initiate_connection(toolkit_slug="metaads", entity_id="safent-one")
    fake.connected_accounts.link.assert_not_called()


@pytest.mark.asyncio
async def test_google_uses_real_managed_metadata_and_supports_more_than_one_connection():
    fake = sdk()
    client = ComposioClient("test", sdk=fake)
    await client.initiate_connection(toolkit_slug="googleads", entity_id="safent-one")
    fake.toolkits.get.assert_called_once_with(slug="googleads")
    fake.auth_configs.create.assert_called_once_with(
        "googleads", {"type": "use_composio_managed_auth"}
    )
    fake.connected_accounts.link.assert_called_once_with(
        "safent-one", "ac-managed", callback_url=None, allow_multiple=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("schemes", [None, [], ["API_KEY"]])
async def test_oauth_supported_is_not_evidence_of_managed_oauth(schemes):
    fake = sdk()
    fake.toolkits.get.return_value = NS(
        slug="metaads", enabled=True, composio_managed_auth_schemes=schemes
    )
    client = ComposioClient("test", sdk=fake)
    with pytest.raises(ComposioApiError):
        await client.initiate_connection(toolkit_slug="metaads", entity_id="safent-one")
    fake.auth_configs.create.assert_not_called()
    fake.connected_accounts.link.assert_not_called()


@pytest.mark.asyncio
async def test_catalog_distinguishes_google_managed_from_meta_setup():
    fake = sdk()
    fake.toolkits.list.return_value = NS(
        items=[
            NS(
                slug="googleads",
                name="Google Ads",
                meta=NS(description=""),
                auth_schemes=["OAUTH2"],
                composio_managed_auth_schemes=["OAUTH2"],
            ),
            NS(
                slug="metaads",
                name="Meta Ads",
                meta=NS(description=""),
                auth_schemes=["OAUTH2"],
                composio_managed_auth_schemes=[],
            ),
        ]
    )
    items = await ComposioClient("test", sdk=fake).list_toolkits()
    assert items[0].managed_auth_available is True and items[0].oauth_simple
    assert (
        items[1].managed_auth_available is False
        and not items[1].oauth_simple
        and items[1].setup_required
    )
    items = await ComposioClient(
        "test", sdk=fake, auth_config_ids={"metaads": "ac-owned"}
    ).list_toolkits()
    assert items[1].oauth_simple and not items[1].setup_required


@pytest.mark.asyncio
async def test_connected_accounts_filters_foreign_entity_even_if_vendor_returns_it():
    fake = sdk()
    fake.connected_accounts.list.return_value = NS(
        items=[
            NS(id="ca-owned", user_id="one", status="ACTIVE", toolkit=NS(slug="metaads")),
            NS(id="ca-other", user_id="two", status="ACTIVE", toolkit=NS(slug="metaads")),
        ]
    )
    result = await ComposioClient("test", sdk=fake).list_connected_accounts("one")
    assert [account.id for account in result] == ["ca-owned"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["two", "", None])
async def test_delete_never_accepts_foreign_entity(entity):
    fake = sdk()
    fake.connected_accounts.get.return_value = NS(id="ca-other", user_id=entity)
    with pytest.raises(ComposioApiError):
        await ComposioClient("test", sdk=fake).delete_connection("ca-other", entity_id="one")
    fake.connected_accounts.delete.assert_not_called()


@pytest.mark.asyncio
async def test_confirmation_projects_metadata_never_oauth_secrets():
    fake = sdk()
    fake.connected_accounts.get.return_value = NS(
        id="ca-owned",
        user_id="one",
        toolkit=NS(slug="metaads"),
        status="ACTIVE",
        auth_config=NS(id="ac-owned"),
        state={"access_token": "NEVER-RETURN-TOKEN"},
    )
    account = await ComposioClient("test", sdk=fake).get_connected_account(
        "ca-owned", entity_id="one"
    )
    assert account.auth_config_id == "ac-owned" and account.status == "ACTIVE"
    assert "NEVER-RETURN-TOKEN" not in repr(account)


@pytest.mark.asyncio
async def test_confirmation_rejects_foreign_account():
    fake = sdk()
    fake.connected_accounts.get.return_value = NS(id="ca-other", user_id="two")
    with pytest.raises(ComposioApiError):
        await ComposioClient("test", sdk=fake).get_connected_account("ca-other", entity_id="one")
