"""composio_tool_specs: READ vs WRITE classification + spec building.

No network calls — ComposioClient.list_connected_accounts and list_tools
are monkeypatched with in-memory fakes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# ENV-DRIFT GUARD: the product image ships composio>=1.0.0-rc2, which exposes
# `composio.exceptions.ComposioError`. composio_client.py (imported transitively via
# composio_tool_specs) imports that symbol at module load. Older host SDKs (0.7.x)
# lack it, so the import fails on a drifted host. This is dependency drift, NOT a
# product bug — the source is correct for the baked image. Skip the whole module
# where the SDK is too old; run it wherever the product's SDK is installed.
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

from hermes.domain.tool_spec import ToolRisk
from hermes.integrations.composio.composio_client import (
    ConnectedAccountInfo,
    ToolInfo,
)
from hermes.runtime.composio_config_source import ComposioCredential
from hermes.runtime.composio_tool_specs import (
    build_composio_tool_specs,
    classify_tool_risk,
)

pytestmark = pytest.mark.unit


def _make_mock_broker():
    """Return a minimal broker mock sufficient for spec-building tests."""
    from hermes.capabilities.domain.ports import ConsentContext, ExecutionOutcome, ExecutionStatus
    broker = MagicMock()
    outcome = ExecutionOutcome(
        proposal_id=uuid4(),
        status=ExecutionStatus.EXECUTED,
        result={"data": "email_body"},
    )
    broker.dispatch = AsyncMock(return_value=outcome)
    return broker


def _make_mock_consent_context():
    from hermes.capabilities.domain.ports import ConsentContext
    return ConsentContext(tenant_id=uuid4(), operator_id=uuid4())


# ----------------------------------------------------------------
# classify_tool_risk
# ----------------------------------------------------------------

READ_SLUGS = [
    "GMAIL_GET_EMAIL",
    "GOOGLEDRIVE_LIST_FILES",
    "SLACK_FETCH_MESSAGES",
    "GITHUB_SEARCH_REPOSITORIES",
    "NOTION_FIND_PAGE",
    "CALENDAR_READ_EVENTS",
    "HUBSPOT_RETRIEVE_CONTACT",
    "JIRA_VIEW_ISSUE",
    "POSTGRES_QUERY_DATABASE",
]

# EXPORT and DOWNLOAD are intentionally NOT in READ_SLUGS.
# Security note (Fix-4 / CTRL-5, composio_tool_specs.py line 55):
#   EXPORT/DOWNLOAD transfer potentially large data volumes out of the external
#   service into the agent's context — viable exfiltration vector if the cycle
#   is tainted. Classification: WRITE_PROPOSAL (requires broker consent + audit).
WRITE_SLUGS = [
    "GMAIL_SEND_EMAIL",
    "GMAIL_DELETE_EMAIL",
    "SLACK_POST_MESSAGE",
    "GITHUB_CREATE_ISSUE",
    "GITHUB_UPDATE_FILE",
    "GOOGLEDRIVE_UPLOAD_FILE",
    "CALENDAR_CREATE_EVENT",
    "STRIPE_CREATE_PAYMENT",
    "NOTION_CREATE_PAGE",
    "DROPBOX_EXPORT_FILE",       # EXPORT is WRITE_PROPOSAL — see security note above
    "DROPBOX_DOWNLOAD_FILE",     # DOWNLOAD is also WRITE_PROPOSAL (same rationale)
    "UNKNOWN_TOOL",
    "TOOLKIT_ONLY",              # single segment after prefix → write
]


@pytest.mark.parametrize("slug", READ_SLUGS)
def test_read_slugs_classified_as_read_only(slug: str) -> None:
    assert classify_tool_risk(slug) == ToolRisk.READ_ONLY


@pytest.mark.parametrize("slug", WRITE_SLUGS)
def test_write_slugs_classified_as_write_proposal(slug: str) -> None:
    assert classify_tool_risk(slug) == ToolRisk.WRITE_PROPOSAL


# ----------------------------------------------------------------
# build_composio_tool_specs
# ----------------------------------------------------------------

_CRED = ComposioCredential(api_key="csk-test", entity_id="ent-1")

_FAKE_ACCOUNTS = [
    ConnectedAccountInfo(
        id="acc-1", toolkit_slug="GMAIL", entity_id="ent-1", status="ACTIVE"
    ),
]

_FAKE_TOOLS = [
    ToolInfo(
        slug="GMAIL_GET_EMAIL",
        description="Fetch an email",
        input_parameters={
            "type": "object",
            "properties": {"email_id": {"type": "string"}},
        },
    ),
    ToolInfo(
        slug="GMAIL_SEND_EMAIL",
        description="Send an email",
        input_parameters={
            "type": "object",
            "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
        },
    ),
]


@pytest.mark.asyncio
async def test_read_tool_has_handler_and_read_only_risk() -> None:
    with (
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_connected_accounts",
            new_callable=AsyncMock,
            return_value=_FAKE_ACCOUNTS,
        ),
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_tools",
            new_callable=AsyncMock,
            return_value=_FAKE_TOOLS,
        ),
    ):
        specs = await build_composio_tool_specs(_CRED, broker=_make_mock_broker(), consent_context=_make_mock_consent_context())

    read_spec = next(s for s in specs if s.name == "gmail_get_email")
    assert read_spec.risk == ToolRisk.READ_ONLY
    assert read_spec.handler is not None


@pytest.mark.asyncio
async def test_write_tool_has_no_handler_and_write_proposal_risk() -> None:
    with (
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_connected_accounts",
            new_callable=AsyncMock,
            return_value=_FAKE_ACCOUNTS,
        ),
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_tools",
            new_callable=AsyncMock,
            return_value=_FAKE_TOOLS,
        ),
    ):
        specs = await build_composio_tool_specs(_CRED, broker=_make_mock_broker(), consent_context=_make_mock_consent_context())

    write_spec = next(s for s in specs if s.name == "gmail_send_email")
    assert write_spec.risk == ToolRisk.WRITE_PROPOSAL
    assert write_spec.handler is None


@pytest.mark.asyncio
async def test_returns_empty_when_no_active_accounts() -> None:
    inactive = [
        ConnectedAccountInfo(
            id="acc-x", toolkit_slug="SLACK", entity_id="ent-1", status="INACTIVE"
        )
    ]
    with patch(
        "hermes.runtime.composio_tool_specs.ComposioClient.list_connected_accounts",
        new_callable=AsyncMock,
        return_value=inactive,
    ):
        specs = await build_composio_tool_specs(_CRED, broker=_make_mock_broker(), consent_context=_make_mock_consent_context())

    assert specs == ()


@pytest.mark.asyncio
async def test_fails_soft_when_tool_fetch_raises() -> None:
    from hermes.integrations.composio.composio_client import ComposioApiError  # noqa: PLC0415

    with (
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_connected_accounts",
            new_callable=AsyncMock,
            return_value=_FAKE_ACCOUNTS,
        ),
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_tools",
            new_callable=AsyncMock,
            side_effect=ComposioApiError(500, "internal error"),
        ),
    ):
        specs = await build_composio_tool_specs(_CRED, broker=_make_mock_broker(), consent_context=_make_mock_consent_context())

    assert specs == ()


@pytest.mark.asyncio
async def test_all_specs_have_composio_tag() -> None:
    with (
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_connected_accounts",
            new_callable=AsyncMock,
            return_value=_FAKE_ACCOUNTS,
        ),
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_tools",
            new_callable=AsyncMock,
            return_value=_FAKE_TOOLS,
        ),
    ):
        specs = await build_composio_tool_specs(_CRED, broker=_make_mock_broker(), consent_context=_make_mock_consent_context())

    assert all("composio" in s.tags for s in specs)


@pytest.mark.asyncio
async def test_read_handler_routes_through_broker() -> None:
    """Handler for READ tools must route through broker.dispatch (KC-4 fix).

    The handler must NOT call execute_action directly — all Composio I/O must
    pass through broker.dispatch (consent + audit + kill-switch).
    """
    from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
    from uuid import uuid4

    mock_broker = MagicMock()
    mock_outcome = ExecutionOutcome(
        proposal_id=uuid4(),
        status=ExecutionStatus.EXECUTED,
        result={"data": "email_body"},
    )
    mock_broker.dispatch = AsyncMock(return_value=mock_outcome)
    mock_consent = _make_mock_consent_context()

    with (
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_connected_accounts",
            new_callable=AsyncMock,
            return_value=_FAKE_ACCOUNTS,
        ),
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.list_tools",
            new_callable=AsyncMock,
            return_value=[_FAKE_TOOLS[0]],  # GET only
        ),
        patch(
            "hermes.runtime.composio_tool_specs.ComposioClient.execute_action",
            new_callable=AsyncMock,
            return_value={"data": "direct_call_should_not_happen"},
        ) as mock_exec,
    ):
        specs = await build_composio_tool_specs(
            _CRED, broker=mock_broker, consent_context=mock_consent
        )
        read_spec = specs[0]
        result = await read_spec.handler({"email_id": "msg-123"})  # type: ignore[misc]

    # Broker must be called exactly once — the handler routes through broker.dispatch.
    mock_broker.dispatch.assert_called_once()
    proposal = mock_broker.dispatch.call_args[0][0]
    assert proposal.entity_type == "composio"
    assert "GMAIL_GET_EMAIL" in proposal.parameters.get("slug", "")

    # execute_action must NOT be called directly — that would bypass the broker.
    mock_exec.assert_not_called()

    assert result == {"data": "email_body"}
