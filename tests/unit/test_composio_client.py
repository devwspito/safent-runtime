"""Unit tests for ComposioClient (SDK-backed).

Uses a fake _sdk injected via the optional `sdk=` constructor argument.
No network calls are made.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ENV-DRIFT GUARD: the product image ships composio>=1.0.0-rc2, which exposes
# `composio.exceptions.ComposioError`. composio_client.py imports that symbol at
# module load. Older host SDKs (0.7.x) lack it, so the import fails on a drifted
# host. This is dependency drift, NOT a product bug — the source is correct for the
# baked image. Skip the whole module where the SDK is too old; run it wherever the
# product's SDK is installed (image, matching dev env).
_composio_exceptions = pytest.importorskip(
    "composio.exceptions",
    reason="composio SDK not installed",
)
if not hasattr(_composio_exceptions, "ComposioError"):
    pytest.skip(
        "composio SDK on host lacks composio.exceptions.ComposioError "
        "(product image ships composio>=1.0.0-rc2 which has it) — env drift, "
        "not a product bug",
        allow_module_level=True,
    )

from hermes.integrations.composio.composio_client import (
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
) -> SimpleNamespace:
    meta = SimpleNamespace(description=description)
    return SimpleNamespace(slug=slug, name=name, meta=meta)


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
) -> SimpleNamespace:
    toolkit = SimpleNamespace(slug=toolkit_slug)
    return SimpleNamespace(id=id, toolkit=toolkit, user_id=user_id, status=status)


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

    # tools.execute → ToolExecutionResponse TypedDict
    sdk.tools.execute.return_value = (
        execute_result
        if execute_result is not None
        else {"data": {}, "error": None, "successful": True}
    )

    return sdk


def _client(sdk: MagicMock) -> ComposioClient:
    return ComposioClient(api_key="test-key", sdk=sdk)


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
        import hermes.integrations.composio.composio_client as _mod  # noqa: PLC0415

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
# list_connected_accounts
# ---------------------------------------------------------------------------


class TestListConnectedAccounts:
    @pytest.mark.asyncio
    async def test_maps_items_to_connected_account_info(self) -> None:
        sdk = _fake_sdk(
            connected_account_items=[
                _make_connected_account_item("ca-1", "GMAIL", "user-99", "ACTIVE"),
            ]
        )
        result = await _client(sdk).list_connected_accounts("user-99")

        assert len(result) == 1
        assert result[0] == ConnectedAccountInfo(
            id="ca-1",
            toolkit_slug="GMAIL",
            entity_id="user-99",
            status="ACTIVE",
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


# ---------------------------------------------------------------------------
# delete_connection
# ---------------------------------------------------------------------------


class TestDeleteConnection:
    @pytest.mark.asyncio
    async def test_passes_connection_id_to_sdk(self) -> None:
        sdk = _fake_sdk()
        await _client(sdk).delete_connection("conn-xyz")

        sdk.connected_accounts.delete.assert_called_once_with("conn-xyz")

    @pytest.mark.asyncio
    async def test_returns_none(self) -> None:
        sdk = _fake_sdk()
        result = await _client(sdk).delete_connection("conn-xyz")

        assert result is None


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
    async def test_passes_slug_params_entity_id_to_sdk(self) -> None:
        sdk = _fake_sdk()
        await _client(sdk).execute_action(
            slug="GMAIL_SEND_EMAIL",
            params={"to": "a@b.com"},
            entity_id="user-1",
        )

        sdk.tools.execute.assert_called_once_with(
            "GMAIL_SEND_EMAIL",
            {"to": "a@b.com"},
            user_id="user-1",
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
        with patch("hermes.integrations.composio.composio_client.Composio") as mock_composio:
            mock_composio.return_value = MagicMock()
            client = ComposioClient(api_key="real-key")

        mock_composio.assert_called_once_with(api_key="real-key")
        assert client is not None
