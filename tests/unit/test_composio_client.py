"""Unit tests for ComposioClient (SDK-backed), package `safent_composio`.

Uses a fake `sdk` handle injected via the optional `sdk=` constructor argument.
No network calls are made, except in `TestInjectedTransport`, which proves the
`transport=` wiring (REQ-07) by routing through a real `httpx.MockTransport`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ENV-DRIFT GUARD: the product image pins composio==0.13.1, which exposes
# `composio.exceptions.ComposioError`. safent_composio.client imports that
# symbol at module load. Other host SDK distributions may lack it, so import
# fails on a drifted host. This is dependency drift, NOT a product bug — the
# source is correct for the baked image. Skip the whole module where the SDK
# is too old; run it wherever the product's SDK is installed (image, matching
# dev env).
_composio_exceptions = pytest.importorskip(
    "composio.exceptions",
    reason="composio SDK not installed",
)
if not hasattr(_composio_exceptions, "ComposioError"):
    pytest.skip(
        "composio SDK on host lacks composio.exceptions.ComposioError "
        "(product image pins composio==0.13.1 which has it) — env drift, "
        "not a product bug",
        allow_module_level=True,
    )

from safent_composio import (
    ComposioApiError,
    ComposioClient,
    ConnectedAccountInfo,
    ConnectionInitResult,
    ToolInfo,
    ToolkitInfo,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake SDK builder helpers
# ---------------------------------------------------------------------------


def _make_toolkit_item(
    slug: str,
    name: str,
    description: str,
    *,
    auth_schemes: list[str] | None = None,
    composio_managed_auth_schemes: list[str] | None = None,
) -> SimpleNamespace:
    meta = SimpleNamespace(description=description)
    return SimpleNamespace(
        slug=slug,
        name=name,
        meta=meta,
        auth_schemes=auth_schemes,
        composio_managed_auth_schemes=composio_managed_auth_schemes,
    )


def _make_tool_item(
    slug: str,
    description: str,
    input_parameters: dict[str, Any],
    human_description: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        slug=slug,
        description=description,
        human_description=human_description,
        input_parameters=input_parameters,
    )


def _make_connected_account_item(
    id: str,
    toolkit_slug: str,
    user_id: str,
    status: str,
    *,
    auth_config_id: str | None = None,
) -> SimpleNamespace:
    toolkit = SimpleNamespace(slug=toolkit_slug) if toolkit_slug else None
    auth_config = SimpleNamespace(id=auth_config_id) if auth_config_id else None
    return SimpleNamespace(
        id=id, toolkit=toolkit, user_id=user_id, status=status, auth_config=auth_config
    )


def _make_auth_config_item(id: str, status: str) -> SimpleNamespace:
    return SimpleNamespace(id=id, status=status)


def _make_connection_request(
    id: str,
    redirect_url: str | None,
    status: str,
) -> SimpleNamespace:
    return SimpleNamespace(id=id, redirect_url=redirect_url, status=status)


def _fake_sdk(
    *,
    toolkit_items: list[Any] | None = None,
    tool_items: list[Any] | None = None,
    connected_account_items: list[Any] | None = None,
    auth_config_items: list[Any] | None = None,
    link_result: Any | None = None,
    execute_result: dict[str, Any] | None = None,
    delete_return: Any = None,
    get_connected_account_return: Any = None,
) -> MagicMock:
    sdk = MagicMock()

    # toolkits.list → response with .items
    sdk.toolkits.list.return_value = SimpleNamespace(
        items=toolkit_items or []
    )

    # tools.get_raw_composio_tools → list[Tool]
    sdk.tools.get_raw_composio_tools.return_value = tool_items or []

    # connected_accounts.list → response with .items
    sdk.connected_accounts.list.return_value = SimpleNamespace(
        items=connected_account_items or []
    )

    # connected_accounts.delete → void
    sdk.connected_accounts.delete.return_value = delete_return

    # connected_accounts.get → single account (retrieve/ownership checks).
    # Auto-echoes an owned account by default so pre-existing tests that
    # don't care about ownership plumbing keep working unmodified; tests that
    # DO care pass get_connected_account_return explicitly.
    sdk.connected_accounts.get.side_effect = (
        lambda connection_id: get_connected_account_return
        if get_connected_account_return is not None
        else SimpleNamespace(id=connection_id, user_id="user-1")
    )

    # connected_accounts.link → ConnectionRequest
    sdk.connected_accounts.link.return_value = (
        link_result
        if link_result is not None
        else _make_connection_request("conn-1", "https://oauth.example.com/auth", "INITIATED")
    )

    # auth_configs.list → response with .items
    sdk.auth_configs.list.return_value = SimpleNamespace(
        items=auth_config_items or []
    )

    # auth_configs.create → auth config with .id
    sdk.auth_configs.create.return_value = SimpleNamespace(id="ac-new-1")

    # auth_configs.get → single auth config (validate_auth_config)
    sdk.auth_configs.get.return_value = SimpleNamespace(
        id="ac-1", status="ENABLED", auth_scheme="OAUTH2", toolkit=SimpleNamespace(slug="gmail")
    )

    # tools.execute → ToolExecutionResponse TypedDict
    sdk.tools.execute.return_value = (
        execute_result
        if execute_result is not None
        else {"data": {}, "error": None, "successful": True}
    )

    return sdk


def _client(sdk: MagicMock, **kwargs: Any) -> ComposioClient:
    return ComposioClient(api_key="test-key", sdk=sdk, **kwargs)


# ---------------------------------------------------------------------------
# list_toolkits
# ---------------------------------------------------------------------------


class TestListToolkits:
    @pytest.mark.asyncio
    async def test_maps_items_to_toolkit_info(self) -> None:
        sdk = _fake_sdk(
            toolkit_items=[
                _make_toolkit_item("GMAIL", "Gmail", "Google email service"),
                _make_toolkit_item("SLACK", "Slack", "Messaging platform"),
            ]
        )
        result = await _client(sdk).list_toolkits()

        assert len(result) == 2
        assert result[0] == ToolkitInfo(slug="GMAIL", name="Gmail", description="Google email service")
        assert result[1] == ToolkitInfo(slug="SLACK", name="Slack", description="Messaging platform")

    @pytest.mark.asyncio
    async def test_filters_by_search_term(self) -> None:
        sdk = _fake_sdk(
            toolkit_items=[
                _make_toolkit_item("GMAIL", "Gmail", "Google email service"),
                _make_toolkit_item("SLACK", "Slack", "Messaging platform"),
            ]
        )
        result = await _client(sdk).list_toolkits(search="gmail")

        assert len(result) == 1
        assert result[0].slug == "GMAIL"

    @pytest.mark.asyncio
    async def test_skips_items_with_empty_slug(self) -> None:
        sdk = _fake_sdk(
            toolkit_items=[
                _make_toolkit_item("", "No Slug", "desc"),
                _make_toolkit_item("GITHUB", "GitHub", "Code hosting"),
            ]
        )
        result = await _client(sdk).list_toolkits()

        assert len(result) == 1
        assert result[0].slug == "GITHUB"

    @pytest.mark.asyncio
    async def test_passes_limit_as_float_to_sdk(self) -> None:
        sdk = _fake_sdk()
        await _client(sdk).list_toolkits(limit=25)

        sdk.toolkits.list.assert_called_once_with(limit=25.0, sort_by="usage")

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_items(self) -> None:
        sdk = _fake_sdk(toolkit_items=[])
        result = await _client(sdk).list_toolkits()

        assert result == []

    @pytest.mark.asyncio
    async def test_managed_auth_available_when_provider_declares_schemes(self) -> None:
        sdk = _fake_sdk(
            toolkit_items=[
                _make_toolkit_item(
                    "GMAIL",
                    "Gmail",
                    "d",
                    auth_schemes=["OAUTH2"],
                    composio_managed_auth_schemes=["OAUTH2"],
                )
            ]
        )
        result = await _client(sdk).list_toolkits()

        assert result[0].oauth_simple is True
        assert result[0].managed_auth_available is True
        assert result[0].setup_required is False

    @pytest.mark.asyncio
    async def test_ads_toolkit_needs_setup_without_a_selected_auth_config(self) -> None:
        sdk = _fake_sdk(
            toolkit_items=[
                _make_toolkit_item(
                    "GOOGLEADS", "Google Ads", "d",
                    auth_schemes=["OAUTH2"], composio_managed_auth_schemes=None,
                )
            ]
        )
        result = await _client(sdk).list_toolkits()

        assert result[0].oauth_simple is False
        assert result[0].setup_required is True

    @pytest.mark.asyncio
    async def test_ads_toolkit_is_simple_once_owner_selected_an_auth_config(self) -> None:
        sdk = _fake_sdk(
            toolkit_items=[_make_toolkit_item("GOOGLEADS", "Google Ads", "d")]
        )
        result = await _client(sdk, auth_config_ids={"googleads": "ac-owned"}).list_toolkits()

        assert result[0].oauth_simple is True
        assert result[0].setup_required is False


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------


class TestListTools:
    @pytest.mark.asyncio
    async def test_maps_tool_items_to_tool_info(self) -> None:
        sdk = _fake_sdk(
            tool_items=[
                _make_tool_item(
                    "GMAIL_GET_EMAIL",
                    "Fetch an email",
                    {"type": "object", "properties": {"id": {"type": "string"}}},
                ),
            ]
        )
        result = await _client(sdk).list_tools("GMAIL")

        assert len(result) == 1
        assert result[0] == ToolInfo(
            slug="GMAIL_GET_EMAIL",
            description="Fetch an email",
            input_parameters={"type": "object", "properties": {"id": {"type": "string"}}},
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_human_description(self) -> None:
        sdk = _fake_sdk(
            tool_items=[
                _make_tool_item(
                    "GMAIL_SEND_EMAIL",
                    "",  # empty description → must fall through to human_description
                    {},
                    human_description="Compose and send an email",
                ),
            ]
        )
        # Use a distinct slug to avoid hitting a warm cache entry from another test.
        result = await _client(sdk).list_tools("GMAIL_HUMAN_DESC_TEST")

        assert result[0].description == "Compose and send an email"

    @pytest.mark.asyncio
    async def test_passes_uppercase_slug_to_sdk(self) -> None:
        sdk = _fake_sdk()
        await _client(sdk).list_tools("gmail")

        sdk.tools.get_raw_composio_tools.assert_called_once_with(
            toolkits=["GMAIL"],
            limit=500,
        )

    @pytest.mark.asyncio
    async def test_caches_result_on_second_call(self) -> None:
        import safent_composio.client as _mod  # noqa: PLC0415

        sdk = _fake_sdk(
            tool_items=[_make_tool_item("GMAIL_GET_EMAIL", "desc", {})]
        )
        client = _client(sdk)

        # Use a unique slug so we don't inherit a warm cache entry from parallel tests.
        slug = "_CACHE_TEST_UNIQUE_SLUG"
        _mod._tool_cache.pop(slug, None)  # ensure cold start
        try:
            await client.list_tools(slug)
            await client.list_tools(slug)
        finally:
            _mod._tool_cache.pop(slug, None)

        # SDK should be called exactly once due to cache.
        assert sdk.tools.get_raw_composio_tools.call_count == 1


# ---------------------------------------------------------------------------
# list_connected_accounts / get_connected_account
# ---------------------------------------------------------------------------


class TestListConnectedAccounts:
    @pytest.mark.asyncio
    async def test_maps_items_to_connected_account_info(self) -> None:
        sdk = _fake_sdk(
            connected_account_items=[
                _make_connected_account_item(
                    "ca-1", "GMAIL", "user-99", "ACTIVE", auth_config_id="ac-1"
                ),
            ]
        )
        result = await _client(sdk).list_connected_accounts("user-99")

        assert len(result) == 1
        assert result[0] == ConnectedAccountInfo(
            id="ca-1",
            toolkit_slug="GMAIL",
            entity_id="user-99",
            status="ACTIVE",
            auth_config_id="ac-1",
        )

    @pytest.mark.asyncio
    async def test_passes_entity_id_and_active_status_to_sdk(self) -> None:
        sdk = _fake_sdk()
        await _client(sdk).list_connected_accounts("user-42")

        sdk.connected_accounts.list.assert_called_once_with(
            user_ids=["user-42"],
            statuses=["ACTIVE"],
        )

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_accounts(self) -> None:
        sdk = _fake_sdk(connected_account_items=[])
        result = await _client(sdk).list_connected_accounts("user-42")

        assert result == []

    @pytest.mark.asyncio
    async def test_handles_missing_toolkit(self) -> None:
        item = _make_connected_account_item("ca-2", "SLACK", "u-1", "ACTIVE")
        item.toolkit = None  # type: ignore[assignment]
        sdk = _fake_sdk(connected_account_items=[item])

        result = await _client(sdk).list_connected_accounts("u-1")

        assert result[0].toolkit_slug == ""

    @pytest.mark.asyncio
    async def test_excludes_accounts_belonging_to_a_different_entity(self) -> None:
        """Defense-in-depth: never trust the SDK query filter alone (CTRL)."""
        mismatched = _make_connected_account_item("ca-3", "GMAIL", "someone-else", "ACTIVE")
        sdk = _fake_sdk(connected_account_items=[mismatched])

        result = await _client(sdk).list_connected_accounts("u-1")

        assert result == []


class TestGetConnectedAccount:
    @pytest.mark.asyncio
    async def test_returns_account_when_entity_matches(self) -> None:
        account = _make_connected_account_item(
            "ca-1", "GMAIL", "user-1", "ACTIVE", auth_config_id="ac-1"
        )
        sdk = _fake_sdk(get_connected_account_return=account)

        result = await _client(sdk).get_connected_account("ca-1", entity_id="user-1")

        assert result == ConnectedAccountInfo(
            id="ca-1",
            toolkit_slug="GMAIL",
            entity_id="user-1",
            status="ACTIVE",
            auth_config_id="ac-1",
        )

    @pytest.mark.asyncio
    async def test_raises_404_when_entity_does_not_match(self) -> None:
        account = _make_connected_account_item("ca-1", "GMAIL", "someone-else", "ACTIVE")
        sdk = _fake_sdk(get_connected_account_return=account)

        with pytest.raises(ComposioApiError) as exc_info:
            await _client(sdk).get_connected_account("ca-1", entity_id="user-1")

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_raises_404_when_entity_id_is_empty(self) -> None:
        account = _make_connected_account_item("ca-1", "GMAIL", "", "ACTIVE")
        sdk = _fake_sdk(get_connected_account_return=account)

        with pytest.raises(ComposioApiError) as exc_info:
            await _client(sdk).get_connected_account("ca-1", entity_id="")

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# validate_auth_config
# ---------------------------------------------------------------------------


class TestValidateAuthConfig:
    @pytest.mark.asyncio
    async def test_returns_info_when_enabled_and_toolkit_matches(self) -> None:
        sdk = _fake_sdk()

        result = await _client(sdk).validate_auth_config("gmail", "ac-1")

        assert result.id == "ac-1"
        assert result.toolkit_slug == "gmail"
        assert result.status == "ENABLED"

    @pytest.mark.asyncio
    async def test_raises_409_on_toolkit_mismatch(self) -> None:
        sdk = _fake_sdk()
        sdk.auth_configs.get.return_value = SimpleNamespace(
            id="ac-1", status="ENABLED", auth_scheme="OAUTH2", toolkit=SimpleNamespace(slug="slack")
        )

        with pytest.raises(ComposioApiError) as exc_info:
            await _client(sdk).validate_auth_config("gmail", "ac-1")

        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_raises_409_when_disabled(self) -> None:
        sdk = _fake_sdk()
        sdk.auth_configs.get.return_value = SimpleNamespace(
            id="ac-1", status="DISABLED", auth_scheme="OAUTH2", toolkit=SimpleNamespace(slug="gmail")
        )

        with pytest.raises(ComposioApiError) as exc_info:
            await _client(sdk).validate_auth_config("gmail", "ac-1")

        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_never_leaks_provider_error_body(self) -> None:
        from composio_client import APIStatusError  # noqa: PLC0415
        import httpx  # noqa: PLC0415

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 403
        mock_response.request = MagicMock(spec=httpx.Request)
        sdk = _fake_sdk()
        sdk.auth_configs.get.side_effect = APIStatusError(
            "forbidden", response=mock_response, body={"credentials": "NEVER-LEAK"}
        )

        with pytest.raises(ComposioApiError) as exc_info:
            await _client(sdk).validate_auth_config("gmail", "ac-1")

        assert "NEVER-LEAK" not in str(exc_info.value)
        assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# delete_connection — REQ-11: confirms ownership, idempotent on 404/410
# ---------------------------------------------------------------------------


class TestDeleteConnection:
    @pytest.mark.asyncio
    async def test_passes_connection_id_to_sdk(self) -> None:
        sdk = _fake_sdk()
        await _client(sdk).delete_connection("conn-xyz", entity_id="user-1")

        sdk.connected_accounts.delete.assert_called_once_with("conn-xyz")

    @pytest.mark.asyncio
    async def test_returns_none(self) -> None:
        sdk = _fake_sdk()
        result = await _client(sdk).delete_connection("conn-xyz", entity_id="user-1")

        assert result is None

    @pytest.mark.asyncio
    async def test_raises_404_when_entity_does_not_match_and_does_not_delete(self) -> None:
        account = _make_connected_account_item("conn-xyz", "GMAIL", "someone-else", "ACTIVE")
        sdk = _fake_sdk(get_connected_account_return=account)

        with pytest.raises(ComposioApiError) as exc_info:
            await _client(sdk).delete_connection("conn-xyz", entity_id="user-1")

        assert exc_info.value.status_code == 404
        sdk.connected_accounts.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_when_already_gone_on_read(self) -> None:
        from composio_client import APIStatusError  # noqa: PLC0415
        import httpx  # noqa: PLC0415

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 404
        mock_response.request = MagicMock(spec=httpx.Request)
        mock_response.text = "not found"
        sdk = _fake_sdk()
        sdk.connected_accounts.get.side_effect = APIStatusError(
            "not found", response=mock_response, body=None
        )

        result = await _client(sdk).delete_connection("conn-gone", entity_id="user-1")

        assert result is None
        sdk.connected_accounts.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_when_already_gone_on_delete(self) -> None:
        from composio_client import APIStatusError  # noqa: PLC0415
        import httpx  # noqa: PLC0415

        account = _make_connected_account_item("conn-race", "GMAIL", "user-1", "ACTIVE")
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 410
        mock_response.request = MagicMock(spec=httpx.Request)
        mock_response.text = "gone"
        sdk = _fake_sdk(get_connected_account_return=account)
        sdk.connected_accounts.delete.side_effect = APIStatusError(
            "gone", response=mock_response, body=None
        )

        result = await _client(sdk).delete_connection("conn-race", entity_id="user-1")

        assert result is None

    @pytest.mark.asyncio
    async def test_propagates_non_not_found_errors_on_read(self) -> None:
        from composio_client import APIStatusError  # noqa: PLC0415
        import httpx  # noqa: PLC0415

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 500
        mock_response.request = MagicMock(spec=httpx.Request)
        mock_response.text = "server error"
        sdk = _fake_sdk()
        sdk.connected_accounts.get.side_effect = APIStatusError(
            "server error", response=mock_response, body=None
        )

        with pytest.raises(ComposioApiError) as exc_info:
            await _client(sdk).delete_connection("conn-x", entity_id="user-1")

        assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# execute_action
# ---------------------------------------------------------------------------


class TestExecuteAction:
    @pytest.mark.asyncio
    async def test_returns_data_on_success(self) -> None:
        sdk = _fake_sdk(
            execute_result={"data": {"subject": "Hello"}, "error": None, "successful": True}
        )
        result = await _client(sdk).execute_action(
            slug="GMAIL_GET_EMAIL",
            params={"email_id": "msg-1"},
            entity_id="user-1",
        )

        assert result == {"subject": "Hello"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("account_id", [None, "connection-selected-by-user"])
    async def test_passes_slug_params_entity_id_to_sdk(self, account_id) -> None:
        sdk = _fake_sdk()
        await _client(sdk).execute_action(
            slug="GMAIL_SEND_EMAIL",
            params={"to": "a@b.com"},
            entity_id="user-1",
            connected_account_id=account_id,
        )

        sdk.tools.execute.assert_called_once_with(
            "GMAIL_SEND_EMAIL",
            {"to": "a@b.com"},
            user_id="user-1",
            connected_account_id=account_id,
        )

    @pytest.mark.asyncio
    async def test_raises_composio_api_error_on_failure(self) -> None:
        sdk = _fake_sdk(
            execute_result={"data": {}, "error": "Action not authorized", "successful": False}
        )
        with pytest.raises(ComposioApiError) as exc_info:
            await _client(sdk).execute_action(
                slug="GMAIL_SEND_EMAIL",
                params={},
                entity_id="user-1",
            )

        assert exc_info.value.status_code == 502
        assert "Action not authorized" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_uses_default_message_when_error_field_empty(self) -> None:
        sdk = _fake_sdk(
            execute_result={"data": {}, "error": None, "successful": False}
        )
        with pytest.raises(ComposioApiError) as exc_info:
            await _client(sdk).execute_action(
                slug="SOME_TOOL",
                params={},
                entity_id="user-1",
            )

        assert "tool execution failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_data_is_none(self) -> None:
        sdk = _fake_sdk(
            execute_result={"data": None, "error": None, "successful": True}
        )
        result = await _client(sdk).execute_action(
            slug="SOME_TOOL",
            params={},
            entity_id="user-1",
        )

        assert result == {}


# ---------------------------------------------------------------------------
# initiate_connection
# ---------------------------------------------------------------------------


class TestInitiateConnection:
    @pytest.mark.asyncio
    async def test_reuses_existing_enabled_auth_config(self) -> None:
        existing_config = _make_auth_config_item("ac-existing", "ENABLED")
        sdk = _fake_sdk(
            auth_config_items=[existing_config],
            link_result=_make_connection_request(
                "conn-1", "https://oauth.example.com/auth", "INITIATED"
            ),
        )
        result = await _client(sdk).initiate_connection(
            toolkit_slug="GMAIL",
            entity_id="user-1",
            redirect_url="https://app.example.com/callback",
        )

        sdk.auth_configs.create.assert_not_called()
        sdk.connected_accounts.link.assert_called_once_with(
            "user-1",
            "ac-existing",
            callback_url="https://app.example.com/callback",
        )
        assert result == ConnectionInitResult(
            connected_account_id="conn-1",
            redirect_url="https://oauth.example.com/auth",
            status="INITIATED",
        )

    @pytest.mark.asyncio
    async def test_creates_new_auth_config_when_none_exist(self) -> None:
        sdk = _fake_sdk(
            auth_config_items=[],
            link_result=_make_connection_request("conn-2", "https://oauth.example.com/auth", "INITIATED"),
        )
        sdk.auth_configs.create.return_value = SimpleNamespace(id="ac-new-1")

        await _client(sdk).initiate_connection(
            toolkit_slug="GMAIL",
            entity_id="user-1",
        )

        sdk.auth_configs.create.assert_called_once_with(
            "gmail",
            {"type": "use_composio_managed_auth"},
        )
        sdk.connected_accounts.link.assert_called_once_with(
            "user-1",
            "ac-new-1",
            callback_url=None,
        )

    @pytest.mark.asyncio
    async def test_skips_disabled_configs_and_creates_new(self) -> None:
        sdk = _fake_sdk(
            auth_config_items=[_make_auth_config_item("ac-disabled", "DISABLED")],
            link_result=_make_connection_request("conn-3", "https://x.example.com", "INITIATED"),
        )
        sdk.auth_configs.create.return_value = SimpleNamespace(id="ac-fresh")

        await _client(sdk).initiate_connection(
            toolkit_slug="SLACK",
            entity_id="user-2",
        )

        sdk.auth_configs.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_maps_connection_request_to_result(self) -> None:
        sdk = _fake_sdk(
            auth_config_items=[_make_auth_config_item("ac-1", "ENABLED")],
            link_result=_make_connection_request(
                "conn-5",
                "https://composio.dev/oauth/GITHUB",
                "INITIATED",
            ),
        )
        result = await _client(sdk).initiate_connection(
            toolkit_slug="GITHUB",
            entity_id="ent-x",
        )

        assert result.connected_account_id == "conn-5"
        assert result.redirect_url == "https://composio.dev/oauth/GITHUB"
        assert result.status == "INITIATED"

    @pytest.mark.asyncio
    async def test_redirect_url_defaults_to_empty_string_when_none(self) -> None:
        sdk = _fake_sdk(
            auth_config_items=[_make_auth_config_item("ac-1", "ENABLED")],
            link_result=_make_connection_request("conn-6", None, "INITIATED"),
        )
        result = await _client(sdk).initiate_connection(
            toolkit_slug="NOTION",
            entity_id="user-1",
        )

        assert result.redirect_url == ""

    @pytest.mark.asyncio
    async def test_uses_pre_known_auth_config_id_without_sdk_round_trip(self) -> None:
        sdk = _fake_sdk(
            link_result=_make_connection_request("conn-7", "https://x", "INITIATED"),
        )
        client = _client(sdk, auth_config_ids={"gmail": "ac-1"})

        await client.initiate_connection(toolkit_slug="GMAIL", entity_id="user-1")

        sdk.auth_configs.list.assert_not_called()
        sdk.auth_configs.create.assert_not_called()
        sdk.connected_accounts.link.assert_called_once_with(
            "user-1",
            "ac-1",
            callback_url=None,
        )


# ---------------------------------------------------------------------------
# _guarded: exception mapping
# ---------------------------------------------------------------------------


class TestGuardedExceptionMapping:
    @pytest.mark.asyncio
    async def test_api_status_error_mapped_to_composio_api_error(self) -> None:
        from composio_client import APIStatusError  # noqa: PLC0415

        import httpx  # noqa: PLC0415

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 403
        mock_response.request = MagicMock(spec=httpx.Request)
        mock_response.text = "Forbidden"

        exc = APIStatusError(
            "Forbidden",
            response=mock_response,
            body={"detail": "Forbidden"},
        )

        sdk = _fake_sdk()
        sdk.toolkits.list.side_effect = exc
        client = _client(sdk)

        with pytest.raises(ComposioApiError) as exc_info:
            await client.list_toolkits()

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_api_error_mapped_to_502(self) -> None:
        from composio_client import APIError  # noqa: PLC0415
        import httpx  # noqa: PLC0415

        exc = APIError(
            "Connection refused",
            MagicMock(spec=httpx.Request),
            body=None,
        )

        sdk = _fake_sdk()
        sdk.toolkits.list.side_effect = exc
        client = _client(sdk)

        with pytest.raises(ComposioApiError) as exc_info:
            await client.list_toolkits()

        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_composio_sdk_error_mapped_to_502(self) -> None:
        from composio.exceptions import ComposioError  # noqa: PLC0415

        exc = ComposioError(message="SDK internal error")

        sdk = _fake_sdk()
        sdk.toolkits.list.side_effect = exc
        client = _client(sdk)

        with pytest.raises(ComposioApiError) as exc_info:
            await client.list_toolkits()

        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_api_status_error_caught_before_api_error(self) -> None:
        """APIStatusError (subclass of APIError) must be caught by the more specific branch."""
        from composio_client import APIStatusError  # noqa: PLC0415
        import httpx  # noqa: PLC0415

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 429
        mock_response.request = MagicMock(spec=httpx.Request)
        mock_response.text = "Rate limited"

        exc = APIStatusError("Rate limited", response=mock_response, body=None)

        sdk = _fake_sdk()
        sdk.connected_accounts.list.side_effect = exc
        client = _client(sdk)

        with pytest.raises(ComposioApiError) as exc_info:
            await client.list_connected_accounts("user-1")

        # Must be 429, not 502 — proves the specific branch fired.
        assert exc_info.value.status_code == 429


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_raises_when_api_key_empty(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            ComposioClient(api_key="")

    def test_accepts_injected_sdk(self) -> None:
        sdk = _fake_sdk()
        client = ComposioClient(api_key="test-key", sdk=sdk)
        assert client._sdk is sdk  # type: ignore[attr-defined]

    def test_constructs_real_sdk_when_no_injection(self) -> None:
        with patch("safent_composio._transport.Composio") as mock_composio:
            mock_composio.return_value = MagicMock()
            client = ComposioClient(api_key="real-key")

        mock_composio.assert_called_once_with(api_key="real-key")
        assert client is not None

    def test_rejects_sdk_and_transport_together(self) -> None:
        import httpx  # noqa: PLC0415

        with pytest.raises(ValueError, match="mutually exclusive"):
            ComposioClient(
                api_key="test-key",
                sdk=_fake_sdk(),
                transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})),
            )


# ---------------------------------------------------------------------------
# Injected transport (REQ-07) — Enterprise's anti-SSRF boundary
# ---------------------------------------------------------------------------


class TestInjectedTransport:
    @pytest.mark.asyncio
    async def test_every_call_routes_through_the_injected_transport(self) -> None:
        """A fake transport must receive: list toolkits, connected accounts,
        initiate (list + link), revoke (get + delete), and tool execution
        (toolkit version lookup + tool metadata + execute)."""
        import httpx  # noqa: PLC0415

        calls: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            path = request.url.path
            is_list_call = path.endswith("/connected_accounts") and request.method == "GET"
            if path.endswith("/toolkits") or is_list_call:
                body = {
                    "items": [],
                    "total_items": 0,
                    "total_pages": 1,
                    "current_page": 1,
                    "next_cursor": None,
                }
            elif path.endswith("/link") and request.method == "POST":
                body = {
                    "connected_account_id": "ca-1",
                    "redirect_url": "https://composio.dev/oauth/gmail",
                    "link_token": "tok",
                    "expires_at": "2030-01-01T00:00:00Z",
                }
            elif path.endswith("/auth_configs/ac-1"):
                body = {
                    "id": "ac-1",
                    "name": "gmail-managed",
                    "no_of_connections": 0,
                    "status": "ENABLED",
                    "tool_access_config": {},
                    "toolkit": {"logo": "", "slug": "gmail"},
                    "type": "default",
                    "uuid": "ac-1",
                    "auth_scheme": "OAUTH2",
                }
            elif path.endswith("/toolkits/gmail"):
                body = {
                    "deprecated": {},
                    "enabled": True,
                    "is_local_toolkit": False,
                    "meta": {
                        "available_versions": ["20260101_00"],
                        "categories": [],
                        "created_at": "2030-01-01T00:00:00Z",
                        "description": "d",
                        "logo": "",
                        "tools_count": 1,
                        "triggers_count": 0,
                        "updated_at": "2030-01-01T00:00:00Z",
                        "version": "1",
                        "app_url": "",
                    },
                    "name": "Gmail",
                    "slug": "gmail",
                    "auth_config_details": [],
                    "auth_guide_url": None,
                    "base_url": None,
                    "composio_managed_auth_schemes": [],
                    "get_current_user_endpoint": None,
                    "get_current_user_endpoint_method": None,
                }
            elif path.endswith("/tools/GMAIL_GET_EMAIL") and request.method == "GET":
                body = {
                    "available_versions": ["20260101_00"],
                    "deprecated": {},
                    "description": "d",
                    "input_parameters": {"type": "object", "properties": {}},
                    "is_deprecated": False,
                    "name": "x",
                    "no_auth": False,
                    "output_parameters": {},
                    "scope_requirements": [],
                    "scopes": [],
                    "slug": "GMAIL_GET_EMAIL",
                    "tags": [],
                    "toolkit": {"slug": "gmail", "name": "Gmail", "logo": ""},
                    "version": "20260101_00",
                    "human_description": "d",
                }
            elif path.endswith("/tools/execute/GMAIL_GET_EMAIL") and request.method == "POST":
                body = {"data": {"subject": "hi"}, "error": None, "successful": True}
            elif request.method == "GET":
                body = {
                    "id": "ca-1",
                    "alias": None,
                    "auth_config": {
                        "id": "ac-1",
                        "auth_scheme": "OAUTH2",
                        "is_composio_managed": True,
                        "is_disabled": False,
                        "deprecated": {},
                    },
                    "created_at": "2030-01-01T00:00:00Z",
                    "data": {},
                    "is_disabled": False,
                    "params": {},
                    "state": {},
                    "status": "ACTIVE",
                    "status_reason": None,
                    "toolkit": {"slug": "gmail"},
                    "updated_at": "2030-01-01T00:00:00Z",
                    "user_id": "ent-1",
                    "word_id": "w-1",
                    "deprecated": {},
                    "experimental": {},
                    "test_request_endpoint": None,
                }
            elif request.method == "DELETE":
                body = {"success": True}
            else:
                body = {}
            return httpx.Response(200, json=body)

        transport = httpx.MockTransport(handler)
        client = ComposioClient(
            api_key="test-key",
            auth_config_ids={"gmail": "ac-1"},  # skip auth_configs round trip
            transport=transport,
        )

        await client.list_toolkits()
        await client.list_connected_accounts("ent-1")
        await client.initiate_connection(toolkit_slug="gmail", entity_id="ent-1")
        await client.delete_connection("ca-1", entity_id="ent-1")
        exec_result = await client.execute_action(
            slug="GMAIL_GET_EMAIL", params={"id": "x"}, entity_id="ent-1"
        )

        methods_and_paths = {(m, p.rsplit("/", 1)[-1]) for m, p in calls}
        assert ("GET", "toolkits") in methods_and_paths
        assert ("GET", "connected_accounts") in methods_and_paths
        assert ("POST", "link") in methods_and_paths
        assert ("DELETE", "ca-1") in methods_and_paths
        assert ("GET", "gmail") in methods_and_paths  # toolkit version lookup
        assert ("POST", "GMAIL_GET_EMAIL") in methods_and_paths  # tools.execute
        assert exec_result == {"subject": "hi"}
        assert len(calls) >= 5  # list, list(dup guard in link), link, get, delete

        # REQ-07 side effect: the transport-pinned handle must never leak
        # tracking events through the SDK's OWN httpx client (bypassing ours)
        # or auto-upload/download files through requests.get on arbitrary URLs.
        from composio.core.models.base import allow_tracking  # noqa: PLC0415

        assert allow_tracking.get() is False
        assert client._sdk.tools._auto_upload_download_files is False  # type: ignore[attr-defined]

    def test_never_trusts_env_proxies_when_transport_injected(self) -> None:
        import httpx  # noqa: PLC0415

        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
        client = ComposioClient(api_key="test-key", transport=transport)

        inner_http_client = client._sdk.toolkits._client._client  # type: ignore[attr-defined]
        assert inner_http_client.trust_env is False

    def test_default_behaviour_unchanged_when_no_transport_injected(self) -> None:
        with patch("safent_composio._transport.Composio") as mock_composio:
            mock_composio.return_value = MagicMock()
            ComposioClient(api_key="real-key")

        mock_composio.assert_called_once_with(api_key="real-key")
