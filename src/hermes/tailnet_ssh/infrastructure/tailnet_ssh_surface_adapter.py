"""TailnetSshSurfaceAdapter — SurfaceAdapterPort for SurfaceKind.TAILNET_SSH.

Executes `tailnet_ssh` / `tailnet_file_get` / `tailnet_file_put` proposals
(dispatched by `op`, injected by `runtime/capability_tool_specs.py` the same
way BROWSER injects `op == tool_name`) after broker routing.

Trust boundary (mirrors `MemorySurfaceAdapter` / `DelegationSurfaceAdapter`):
by the time `replay()` runs, `security_hook._resolve_tailnet_ssh_consent`
(Step 1.6-tailnet_ssh) has ALREADY resolved the per-host authorization —
this adapter performs ZERO additional authorization, it only shapes the
proposal payload into the use case's request VO and runs it. See
`TailnetSshUseCase`'s own docstring for the identical trust-boundary note one
layer down.

The use cases are SYNCHRONOUS (they shell out to `ssh` via
`subprocess.run`, up to `timeout_s` seconds — MAX_TIMEOUT_S = 300). `replay`
is a coroutine (SurfaceAdapterPort contract) running on the broker's event
loop — never block it: every use-case call is off-loaded to a worker thread
via `asyncio.to_thread`.

Capa: infrastructure (adapts the tailnet_ssh application layer to the
SurfaceAdapterPort contract). No framework. I/O only via the injected
use cases.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any
from uuid import UUID

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    SurfaceAdapterPort,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.tailnet_ssh.application.tailnet_file_use_case import (
    TailnetFileGetRequest,
    TailnetFileGetUseCase,
    TailnetFilePutRequest,
    TailnetFilePutUseCase,
)
from hermes.tailnet_ssh.application.tailnet_ssh_use_case import (
    TailnetSshRequest,
    TailnetSshUseCase,
)
from hermes.tailnet_ssh.domain.errors import TailnetSshError
from hermes.tailnet_ssh.tool_names import TAILNET_SSH_TOOL_NAMES

logger = logging.getLogger("hermes.tailnet_ssh.surface_adapter")

_DEFAULT_TIMEOUT_S = 30


class TailnetSshSurfaceAdapter:
    """SurfaceAdapterPort for the TAILNET_SSH surface (spec 022 v2).

    Injected into SurfaceAdapterDispatcher under SurfaceKind.TAILNET_SSH.
    Called by CapabilityBroker.dispatch() for the (already Step-1.6-gated)
    tailnet_ssh / tailnet_file_get / tailnet_file_put proposals.

    Args:
        ssh_use_case: executes `tailnet_ssh`.
        file_get_use_case: executes `tailnet_file_get`.
        file_put_use_case: executes `tailnet_file_put`.
    """

    def __init__(
        self,
        *,
        ssh_use_case: TailnetSshUseCase,
        file_get_use_case: TailnetFileGetUseCase,
        file_put_use_case: TailnetFilePutUseCase,
    ) -> None:
        self._ssh_use_case = ssh_use_case
        self._file_get_use_case = file_get_use_case
        self._file_put_use_case = file_put_use_case

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.TAILNET_SSH

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> CapturedAction:
        """Not used — tailnet_ssh proposals originate from Nous tool calls,
        never from skill-training demonstrations."""
        return CapturedAction(
            surface_kind=self.surface_kind,
            intent_desc=intent_desc,
            payload=params,
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
        )

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        """Dispatch by `op` (== the original tool_name, injected at spec-build
        time) to the matching use case, off the event loop."""
        if action.surface_kind != self.surface_kind:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=f"TailnetSshSurfaceAdapter cannot handle surface_kind={action.surface_kind!r}",
            )

        op = action.payload.get("op")
        if op not in TAILNET_SSH_TOOL_NAMES:
            return ReplayOutcome.rejected_by_policy(
                action.action_id, reason=f"tailnet_ssh op={op!r} not supported"
            )

        try:
            if op == "tailnet_ssh":
                return await self._replay_ssh(action)
            if op == "tailnet_file_get":
                return await self._replay_file_get(action)
            return await self._replay_file_put(action)
        except TailnetSshError as exc:
            logger.warning(
                "hermes.tailnet_ssh.surface_adapter.rejected op=%s error=%s", op, exc
            )
            return ReplayOutcome.failed(action.action_id, error=str(exc))

    async def _replay_ssh(self, action: CapturedAction) -> ReplayOutcome:
        payload = action.payload
        request = TailnetSshRequest(
            host=str(payload.get("host", "")),
            command=str(payload.get("command", "")),
            timeout_s=int(payload.get("timeout_s", _DEFAULT_TIMEOUT_S)),
            stdin=payload.get("stdin"),
        )
        result = await asyncio.to_thread(self._ssh_use_case.execute, request)
        return ReplayOutcome.ok(action.action_id, result=dataclasses.asdict(result))

    async def _replay_file_get(self, action: CapturedAction) -> ReplayOutcome:
        payload = action.payload
        request = TailnetFileGetRequest(
            host=str(payload.get("host", "")), path=str(payload.get("path", ""))
        )
        result = await asyncio.to_thread(self._file_get_use_case.execute, request)
        return ReplayOutcome.ok(action.action_id, result=dataclasses.asdict(result))

    async def _replay_file_put(self, action: CapturedAction) -> ReplayOutcome:
        payload = action.payload
        request = TailnetFilePutRequest(
            host=str(payload.get("host", "")),
            path=str(payload.get("path", "")),
            content=str(payload.get("content", "")),
        )
        result = await asyncio.to_thread(self._file_put_use_case.execute, request)
        return ReplayOutcome.ok(action.action_id, result=dataclasses.asdict(result))

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        """Deterministic bytes for HMAC signing (SkillSigner). tailnet_ssh
        proposals are never captured as skill-training steps today, but the
        contract requires this method — canonicalize the same way the other
        adapters do (sorted-key payload repr)."""
        payload_repr = repr(sorted(action.payload.items()))
        return f"{action.surface_kind.value}:{payload_repr}".encode()
