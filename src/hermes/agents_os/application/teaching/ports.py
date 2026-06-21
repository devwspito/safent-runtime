"""Ports (abstractions) for the teaching context subsystem (spec 004 / US3).

Adapters live in:
  - infrastructure/agent_browser_teaching_context.py  (real, uses AgentBrowserCli)
  - agents_os/testing/fake_teaching_context.py        (test fake, no browser)
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from hermes.agents_os.application.teaching.teaching_context import (
    SurfaceKind,
    TeachingContext,
)


class TeachingContextFactory(Protocol):
    """Port: create / close isolated teaching contexts (FR-003)."""

    def open(  # noqa: A003
        self,
        *,
        teaching_session_id: UUID,
        surface_kind: SurfaceKind,
        tenant_id: UUID,
        operator_id: UUID,
        site_id: str,
    ) -> TeachingContext:
        """Open a new isolated context for a teaching session.

        Returns the TeachingContext VO; the adapter is responsible for
        spawning the browser/display process with an isolated session name
        so it never shares input with any execution context.
        """
        ...

    def close(self, context_id: UUID) -> None:
        """Tear down the context and release associated resources."""
        ...
