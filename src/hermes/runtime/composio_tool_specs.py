"""Build ToolSpec list from connected Composio accounts.

Rules:
  - Only tools from CONNECTED apps are built (not the full 20k catalog).
  - READ actions (GET/LIST/FETCH/SEARCH/FIND) → READ_ONLY with a broker-
    dispatching handler.  The broker applies consent + audit + kill-switch
    before delegating to ComposioSurfaceAdapter.execute.  The result is
    returned to the LLM.  The cycle taint (CTRL-5) is activated by the
    "composio" tag in CapturingToolHost._is_untrusted_read.
  - WRITE actions (SEND/CREATE/UPDATE/DELETE/SET/POST/PUT + any not
    clearly read) → WRITE_PROPOSAL, handler=None.  Goes through the
    CapabilityBroker / HITL gate.  NEVER executed directly.
  - Classification is conservative: unknown verbs → WRITE_PROPOSAL.
  - Errors while fetching tool lists are logged and that app is skipped
    (fail-soft per app, not per run_cycle).

Security fix KC-4:
  Previously READ handlers called ComposioClient.execute_action directly,
  bypassing the CapabilityBroker (no consent, no audit, no kill-switch).
  Now every READ handler dispatches through broker.dispatch, which applies
  the full 8-step gate (CTRL-1..14).  The broker resolves the Composio slug
  via ComposioCapabilityRegistry → ComposioSurfaceAdapter.replay.

  broker and consent_context are injected at build time (from the runtime
  entrypoint / agent loop orchestrator).  When not provided, _make_read_handler
  returns a fail-closed handler that raises on invocation (logs ERROR).
  All callers MUST always provide broker + consent_context.

Called from __main__._build_tool_specs after native OS tools.
Concatenated only when a valid ComposioCredential is available.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from hermes.domain.tool_spec import ToolRisk, ToolSpec
from hermes.integrations.composio.composio_client import (
    ComposioApiError,
    ComposioClient,
    ToolInfo,
)
from hermes.runtime.composio_config_source import ComposioCredential

if TYPE_CHECKING:
    from hermes.capabilities.domain.ports import CapabilityBrokerPort, ConsentContext

logger = logging.getLogger("hermes.runtime.composio_tools")

# Verb prefixes that classify an action as READ_ONLY.
# Convention from Composio slugs: GMAIL_GET_EMAIL, GOOGLEDRIVE_LIST_FILES, etc.
#
# Security note (Fix-4 / CTRL-5):
#   EXPORT and DOWNLOAD are intentionally excluded from the READ_ONLY allow-list.
#   Although they do not create/modify remote resources, they transfer potentially
#   large data volumes out of the external service into the agent's context. This
#   makes them a viable exfiltration vector if the cycle is already tainted.
#   Classification: WRITE_PROPOSAL (requires broker consent + audit), not READ_ONLY.
#   If a specific EXPORT/DOWNLOAD action is safe for auto-execution in a vertical,
#   it can be added to the READ_ONLY set after explicit security-engineer review.
_READ_VERBS = frozenset(
    {
        "GET",
        "LIST",
        "FETCH",
        "SEARCH",
        "FIND",
        "READ",
        "SHOW",
        "VIEW",
        "QUERY",
        "DESCRIBE",
        "RETRIEVE",
        "CHECK",
        "PREVIEW",
        "INSPECT",
        "STATUS",
        "PING",
    }
)


def classify_tool_risk(slug: str) -> ToolRisk:
    """Derive ToolRisk from the action verb embedded in the slug.

    Composio slug format: TOOLKIT_VERB_NOUN  (e.g. GMAIL_SEND_EMAIL).
    Conservative default: anything not clearly read → WRITE_PROPOSAL.
    """
    _MIN_SEGMENTS = 2
    parts = slug.upper().split("_")
    # parts[0] = toolkit name, parts[1] = verb (when slug has 3+ segments)
    if len(parts) >= _MIN_SEGMENTS:
        verb = parts[1]
        if verb in _READ_VERBS:
            return ToolRisk.READ_ONLY
    return ToolRisk.WRITE_PROPOSAL


def _make_read_handler(
    api_key: str,
    entity_id: str,
    slug: str,
    *,
    broker: "CapabilityBrokerPort",
    consent_context: "ConsentContext | None" = None,
    connected_account_id: str | None = None,
) -> Any:
    """Return an async callable that executes a READ action via the broker.

    Security fix KC-4: routes through broker.dispatch (consent + audit +
    kill-switch) instead of calling ComposioClient.execute_action directly.

    broker is required — a broker-less Composio READ spec is unconstructable.
    When consent_context is None (edge case in test/init paths), falls back
    to the fail-closed handler that raises on invocation.

    connected_account_id: forwarded to the broker handler so the exact Composio
    account is pinned in the ToolCallProposal parameters (B1 fix).
    """
    if consent_context is not None:
        # KC-4: broker-dispatching path. Imported from dedicated module to
        # decouple from the composio SDK import chain (composio_broker_handler
        # has zero SDK dependencies so it works in all test environments).
        from hermes.runtime.composio_broker_handler import make_broker_read_handler  # noqa: PLC0415
        return make_broker_read_handler(
            slug=slug,
            entity_id=entity_id,
            broker=broker,
            consent_context=consent_context,
            connected_account_id=connected_account_id,
        )

    logger.error(
        "hermes.composio_tools.no_consent_context_for_read: slug=%s — "
        "consent_context not provided; returning fail-closed handler. "
        "This is a wiring bug. Always provide consent_context.",
        slug,
    )
    return _make_direct_composio_handler(api_key=api_key, entity_id=entity_id, slug=slug)


def _make_broker_read_handler(
    *,
    slug: str,
    entity_id: str,
    broker: CapabilityBrokerPort,
    consent_context: ConsentContext,
    connected_account_id: str | None = None,
) -> Any:
    """Alias — delegates to composio_broker_handler.make_broker_read_handler.

    Kept here for backwards-compat callsites and tests that import from this module.
    """
    from hermes.runtime.composio_broker_handler import make_broker_read_handler  # noqa: PLC0415
    return make_broker_read_handler(
        slug=slug,
        entity_id=entity_id,
        broker=broker,
        consent_context=consent_context,
        connected_account_id=connected_account_id,
    )


def _make_direct_composio_handler(
    *,
    api_key: str,
    entity_id: str,
    slug: str,
) -> Any:
    """Fail-closed stub: never calls Composio API without a broker.

    SECURITY: calling Composio READ actions without a broker bypasses
    consent/audit/kill-switch gates. This function now returns a handler
    that raises, so callers that omit broker+consent_context fail loudly
    instead of silently exfiltrating data.

    All production callers MUST provide broker+consent_context so that
    _make_read_handler takes the broker path and this function is never
    reached. Any call that reaches this function is a wiring bug.
    """

    async def _fail_closed_handler(params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(
            f"hermes.composio_tools.no_broker_fail_closed: slug={slug} — "
            "broker is None; Composio READ action blocked to prevent "
            "ungated API call. Wire broker+consent_context before building specs."
        )

    return _fail_closed_handler


def _tool_info_to_spec(
    tool: ToolInfo,
    *,
    api_key: str,
    entity_id: str,
    broker: "CapabilityBrokerPort",
    consent_context: "ConsentContext | None" = None,
    connected_account_id: str | None = None,
    name_suffix: str = "",
) -> ToolSpec:
    """Convert a Composio ToolInfo to a ToolSpec.

    broker is required — a broker-less Composio READ spec is unconstructable.
    consent_context is passed through to _make_read_handler (KC-4 fix).

    connected_account_id: when provided, pins the exact account in the handler
    (B1 fix) and appends name_suffix to disambiguate specs for multi-account
    toolkits so the LLM can pick the right account.
    """
    risk = classify_tool_risk(tool.slug)

    handler = None
    if risk == ToolRisk.READ_ONLY:
        handler = _make_read_handler(
            api_key=api_key,
            entity_id=entity_id,
            slug=tool.slug,
            broker=broker,
            consent_context=consent_context,
            connected_account_id=connected_account_id,
        )

    spec_name = tool.slug.lower() + (f"__{name_suffix}" if name_suffix else "")
    tags: tuple[str, ...] = ("composio",)
    if connected_account_id:
        tags = ("composio", f"ca:{connected_account_id}")

    return ToolSpec(
        name=spec_name,
        description=tool.description or f"Composio action {tool.slug}",
        parameters_schema=tool.input_parameters or {"type": "object", "properties": {}},
        risk=risk,
        entity_type="composio",
        handler=handler,
        tags=tags,
    )


async def build_composio_tool_specs(
    credential: ComposioCredential,
    *,
    broker: "CapabilityBrokerPort",
    consent_context: "ConsentContext | None" = None,
) -> tuple[ToolSpec, ...]:
    """Build ToolSpec instances for all tools of connected apps.

    Steps:
      1. List connected accounts for credential.entity_id.
      2. Deduplicate by toolkit_slug.
      3. For each connected app, fetch tool list (cached 1h).
      4. Classify each tool; build ToolSpec.

    Args:
        credential:      Composio API credentials.
        broker:          CapabilityBroker — routes READ actions through the
                         full gate (consent + audit + kill-switch).  KC-4 fix:
                         MUST be provided in production. When absent, falls
                         back to direct Composio call (legacy/transition only).
        consent_context: ConsentContext for the current agent cycle — passed
                         to broker.dispatch for each READ action.

    Errors are fail-soft per app so one broken app does not block the rest.
    Returns an empty tuple when no apps are connected or all fetches fail.
    """
    client = ComposioClient(api_key=credential.api_key)

    try:
        accounts = await client.list_connected_accounts(credential.entity_id)
    except ComposioApiError as exc:
        logger.warning(
            "hermes.composio_tools.connected_accounts_failed",
            extra={"error": str(exc)},
        )
        return ()

    active_apps = {
        a.toolkit_slug
        for a in accounts
        if a.status.upper() == "ACTIVE" and a.toolkit_slug
    }

    if not active_apps:
        logger.info("hermes.composio_tools.no_active_apps")
        return ()

    specs: list[ToolSpec] = []
    for app_slug in sorted(active_apps):
        try:
            tools = await client.list_tools(app_slug)
        except ComposioApiError as exc:
            logger.warning(
                "hermes.composio_tools.tool_fetch_failed",
                extra={"app": app_slug, "error": str(exc)},
            )
            continue

        for tool in tools:
            try:
                spec = _tool_info_to_spec(
                    tool,
                    api_key=credential.api_key,
                    entity_id=credential.entity_id,
                    broker=broker,
                    consent_context=consent_context,
                )
                specs.append(spec)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hermes.composio_tools.spec_build_failed",
                    extra={"slug": tool.slug, "error": str(exc)},
                )

    logger.info(
        "hermes.composio_tools.built",
        extra={"apps": sorted(active_apps), "tool_count": len(specs)},
    )
    return tuple(specs)
