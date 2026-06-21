"""Broker-dispatching handler for Composio READ actions (KC-4 fix).

This module is intentionally separate from composio_tool_specs to avoid
importing the Composio SDK (which has version-specific import paths that
break in non-production environments).

The handler produced here is a pure async callable that routes a Composio
READ through broker.dispatch, gaining:
  - Kill-switch (CTRL-12): aborted if agent_state.is_paused().
  - Consent gate (CTRL-2/13): operator_id None → fail-closed.
  - Audit (CTRL-9): PROPOSAL_EXECUTED entry signed and persisted.
  - Taint propagation (CTRL-5): handled by CapturingToolHost via the
    "composio" tag on the ToolSpec (this module doesn't duplicate that).

Capa: runtime (wires domain ports together, no framework, no I/O directa).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from hermes.capabilities.domain.ports import CapabilityBrokerPort, ConsentContext

logger = logging.getLogger("hermes.runtime.composio_broker_handler")


def make_broker_read_handler(
    *,
    slug: str,
    entity_id: str,
    broker: CapabilityBrokerPort,
    consent_context: ConsentContext,
    connected_account_id: str | None = None,
) -> Any:
    """Return an async callable that routes the Composio READ through broker.dispatch.

    Args:
        slug:                  Composio action slug (e.g. "GMAIL_GET_EMAIL").
        entity_id:             Composio entity_id of the user.
        broker:                CapabilityBroker — applies full 8-step gate.
        consent_context:       ConsentContext for the current agent cycle.
        connected_account_id:  When set, pins the exact Composio account used
                               (B1 fix). Forwarded in parameters so
                               ComposioSurfaceAdapter passes it to the SDK.
                               None → SDK picks the default account for the entity.

    The proposal passed to broker.dispatch has:
        tool_name  = slug.lower()
        parameters = {slug, params, entity_id, connected_account_id}
    """

    async def _broker_handler(params: dict[str, Any]) -> dict[str, Any]:
        from hermes.domain.proposal import ToolCallProposal  # noqa: PLC0415
        from hermes.capabilities.domain.ports import ExecutionStatus  # noqa: PLC0415

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name=slug.lower(),
            tenant_id=consent_context.tenant_id,
            entity_id=entity_id,
            entity_type="composio",
            parameters={
                "slug": slug,
                "params": params,
                "entity_id": entity_id,
                "connected_account_id": connected_account_id,
            },
            justification=f"Composio READ: {slug}",
        )
        outcome = await broker.dispatch(proposal, consent_context)

        if outcome.status is ExecutionStatus.EXECUTED:
            return outcome.result or {}

        logger.warning(
            "hermes.composio_broker_handler.read_rejected: slug=%s status=%s error=%s",
            slug,
            outcome.status,
            outcome.error,
        )
        return {
            "error": f"composio_read_blocked: {outcome.status}",
            "detail": outcome.error or "",
        }

    return _broker_handler
