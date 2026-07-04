"""Inbound delegation REST API (FASE 3, A2A cross-human) — web surface for the
pending-delegation HITL card.

  GET  /api/v1/inbound-delegations                 -> [{message_id, from_employee_id,
                                                          body, issued_at, created_at}]
  POST /api/v1/inbound-delegations/{message_id}     body: {decision: "approve"|"reject"}
                                                     -> {ok, task_id?} | {ok: false, error}

Mirrors approvals_api.py's posture: GET is read-only fail-soft ([] on daemon
unavailable, same as GET /api/v1/approvals/pending); POST is a mutator,
fail-hard 503 on daemon unavailable (CTRL-P1-11).

Both verbs are reached via the shared DbusRuntimeProxy (same pattern as
providers_api.py/roster_api.py) — NOT via AgentControlPlane, since these are
NEW narrowly-scoped D-Bus verbs (ListPendingDelegations/
ResolveInboundDelegation), not part of the HITL approval-gate contract.

authZ: the global webui-bearer middleware (main.py's `_require_operator_token`)
already gates every mutating (`POST`/`PUT`/`PATCH`/`DELETE`) request under
`/api/v1/*` — no per-route auth needed here. GET is read-only metadata, same
posture as GET /api/v1/approvals/pending.

Provenance (CWE-862): resolve_inbound_delegation derives approved_by/
rejected_by ALWAYS from the authenticated D-Bus channel on the daemon side
(shell-server's own service uid, direct-uid path — see
DbusRuntimeServiceWiring._authorize_and_resolve) — NEVER from this request
body. The `decision` field here only selects approve vs. reject.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes.shell_server.cowork.inbound_delegations_api")


class ResolveInboundDelegationBody(BaseModel):
    decision: Literal["approve", "reject"]


def create_inbound_delegations_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/inbound-delegations", tags=["inbound-delegations"])

    @router.get("")
    async def list_pending_inbound_delegations(request: Request) -> list[dict]:
        """Pending inbound delegation cards (read-only, no secrets/signature)."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_list("list_pending_delegations")
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.inbound_delegations.list_unavailable",
                extra={"reason": str(exc)},
            )
            return []

    @router.post("/{message_id}")
    async def resolve_inbound_delegation(
        request: Request, message_id: str, body: ResolveInboundDelegationBody
    ) -> dict:
        """Approve/reject ONE pending inbound delegation card."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_dict(
                "resolve_inbound_delegation", message_id, body.decision, ""
            )
        except AgentUnavailable as exc:
            _raise_503(exc, "resolve_inbound_delegation")
            return {}  # unreachable — _raise_503 always raises

    return router


def _raise_503(exc: AgentUnavailable, operation: str) -> None:
    logger.warning(
        "hermes.inbound_delegations.mutator_unavailable",
        extra={"operation": operation, "reason": str(exc)},
    )
    raise HTTPException(
        status_code=503,
        detail={
            "code": "agent_unavailable",
            "message": "El agente no está disponible. Comprueba que hermes-runtime está activo.",
        },
    ) from exc
