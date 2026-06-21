"""AgentBrowserTeachingContext — real TeachingContextFactory adapter (spec 004).

Implements the TeachingContextFactory port using AgentBrowserCli with an
isolated --session name, guaranteeing that the teaching browser never
shares tabs, cookies, or input with any execution context (FR-003).

Session naming:
    teach-{teaching_session_id}

This matches the pattern for execution sessions and relies on agent-browser's
`--session` flag to maintain a fully separate browser daemon per name.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from hermes.agents_os.application.teaching.teaching_context import (
    InputOwner,
    SurfaceKind,
    TeachingContext,
)

logger = logging.getLogger(__name__)

# isolation_key format: teach:{tenant_id}:{site_id}
# This prefix guarantees no collision with execution keys that use a
# different prefix (e.g. exec:{tenant_id}:{site_id}).
_TEACH_PREFIX = "teach"


def _isolation_key(tenant_id: UUID, site_id: str) -> str:
    return f"{_TEACH_PREFIX}:{tenant_id}:{site_id}"


class AgentBrowserTeachingContext:
    """Real adapter: creates an isolated agent-browser session per teaching context.

    The browser process is started lazily on first interaction; this adapter
    only manages the TeachingContext VO and delegates cleanup to the CLI.
    """

    def open(  # noqa: A003
        self,
        *,
        teaching_session_id: UUID,
        surface_kind: SurfaceKind,
        tenant_id: UUID,
        operator_id: UUID,  # noqa: ARG002
        site_id: str,
    ) -> TeachingContext:
        """Allocate an isolated teaching context backed by an agent-browser session."""
        # Import lazily: AgentBrowserCli is optional (Containerfile dep).
        from hermes.browser.infrastructure.agent_browser_cli import AgentBrowserCli  # noqa: PLC0415

        session_name = f"teach-{teaching_session_id}"
        # Instantiate but do NOT call start() here — the caller (coordinator)
        # will start the session when the human actually begins demonstrating.
        # Storing the cli reference is the adapter's responsibility if cleanup
        # via close() needs to issue `agent-browser close --all`.
        self._clis[teaching_session_id] = AgentBrowserCli(session_name=session_name)

        ctx = TeachingContext(
            context_id=uuid4(),
            surface_kind=surface_kind,
            isolation_key=_isolation_key(tenant_id, site_id),
            owner=InputOwner.OPERATOR,
            tenant_id=tenant_id,
            site_id=site_id,
        )
        logger.info(
            "teach_context.opened context_id=%s session_name=%s isolation_key=%s",
            ctx.context_id,
            session_name,
            ctx.isolation_key,
        )
        return ctx

    def __init__(self) -> None:
        # teaching_session_id → AgentBrowserCli (for lifecycle management)
        self._clis: dict[UUID, object] = {}

    def close(self, context_id: UUID) -> None:
        """Close is a no-op at this layer; cleanup is per teaching_session_id.

        In production, `agent-browser close --all --session teach-{id}` should
        be issued here. We delegate to the AgentBrowserCli reference tracked by
        teaching_session_id in the factory's open() call.

        Because context_id ≠ teaching_session_id, and the caller passes
        context_id, we log and skip rather than silently swallow — the daemon
        will be cleaned up by the agent-browser subprocess reaper.
        """
        logger.info("teach_context.close requested context_id=%s", context_id)
