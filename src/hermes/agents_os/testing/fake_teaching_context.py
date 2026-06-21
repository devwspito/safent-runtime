"""FakeTeachingContext — deterministic TeachingContextFactory for tests.

No browser, no network, no filesystem.  Produces TeachingContext VOs with
predictable isolation_keys so tests can assert collision behaviour without
any external process.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from hermes.agents_os.application.teaching.teaching_context import (
    InputOwner,
    SurfaceKind,
    TeachingContext,
)

_TEACH_PREFIX = "teach"


def _isolation_key(tenant_id: UUID, site_id: str) -> str:
    return f"{_TEACH_PREFIX}:{tenant_id}:{site_id}"


class FakeTeachingContext:
    """In-process teaching context factory for unit tests (no browser)."""

    def __init__(self) -> None:
        self._open_ids: set[UUID] = set()

    def open(  # noqa: A003
        self,
        *,
        teaching_session_id: UUID,  # noqa: ARG002
        surface_kind: SurfaceKind,
        tenant_id: UUID,
        operator_id: UUID,  # noqa: ARG002
        site_id: str,
    ) -> TeachingContext:
        ctx = TeachingContext(
            context_id=uuid4(),
            surface_kind=surface_kind,
            isolation_key=_isolation_key(tenant_id, site_id),
            owner=InputOwner.OPERATOR,
            tenant_id=tenant_id,
            site_id=site_id,
        )
        self._open_ids.add(ctx.context_id)
        return ctx

    def close(self, context_id: UUID) -> None:
        self._open_ids.discard(context_id)

    def is_open(self, context_id: UUID) -> bool:
        return context_id in self._open_ids
