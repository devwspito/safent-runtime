"""HashChainAuditPort — records every tailnet_ssh call on the SAME WORM
hash-chain audit as the rest of the daemon (`AuditHashChainSigner`,
`AuditKind.TAILNET_SSH_EXECUTED`). `command` is stored in full (it is not a
secret and the owner needs it for review); output BODIES never reach this
adapter — `AuditPort.record_ssh_call` only ever receives a `truncated` flag.

Never raises: an audit-write failure must not crash the call whose result
the owner is waiting on — logged and swallowed (fail-soft, matching every
other audit call site in this codebase, e.g.
`tool_sensitivity.sensitivity`'s own fail-soft contract).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner, AuditKind

logger = logging.getLogger("hermes.tailnet_ssh.audit")

_COMMAND_LOG_MAX_CHARS = 500


class HashChainAuditPort:
    def __init__(
        self,
        *,
        signer: AuditHashChainSigner,
        audit_repo: Any,
        tenant_id: UUID | None = None,
        run_coroutine: Callable[[Coroutine[Any, Any, Any]], Any] = asyncio.run,
    ) -> None:
        self._signer = signer
        self._audit_repo = audit_repo
        self._tenant_id = tenant_id
        self._run_coroutine = run_coroutine

    def record_ssh_call(
        self,
        *,
        host: str,
        command: str,
        exit_code: int,
        duration_ms: int,
        truncated: bool,
    ) -> None:
        try:
            self._run_coroutine(
                self._signer.append_and_persist(
                    audit_kind=AuditKind.TAILNET_SSH_EXECUTED,
                    actor="agent",
                    description=f"tailnet_ssh → {host} (exit {exit_code})",
                    payload={
                        "host": host,
                        "command": command[:_COMMAND_LOG_MAX_CHARS],
                        "exit_code": exit_code,
                        "duration_ms": duration_ms,
                        "output_truncated": truncated,
                    },
                    audit_repo=self._audit_repo,
                    tenant_id=self._tenant_id,
                    category="tailnet_ssh",
                )
            )
        except Exception:  # noqa: BLE001 — audit is fail-soft, never blocks the call
            logger.warning(
                "hermes.tailnet_ssh.audit_write_failed host=%s exit_code=%s",
                host, exit_code, exc_info=True,
            )
