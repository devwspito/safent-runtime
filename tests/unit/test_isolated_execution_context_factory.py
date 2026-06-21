"""Unit — IsolatedExecutionContextFactory try/finally safety (feature 006 / T064).

Verifica el invariante de cleanup: release_all_for se ejecuta SIEMPRE en
close(), incluso si _stop_os_resource lanza una excepción. Sin try/finally
un fallo futuro en _stop_os_resource dejaría la superficie reclamada hasta
el próximo reconcile del daemon (surface orphan).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from hermes.execution.application.execution_context_registry import (
    ExecutionContextRegistry,
)
from hermes.execution.domain.ports import (
    ExecutionContextId,
    InputOwnerKind,
    InputSurfaceKind,
)
from hermes.execution.infrastructure.isolated_execution_context_factory import (
    IsolatedExecutionContextFactory,
)

pytestmark = pytest.mark.unit


def _make_context_id() -> ExecutionContextId:
    return ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.AGENT_TASK)


class TestCloseReleaseAlwaysRuns:
    """release_all_for runs even when _stop_os_resource raises."""

    @pytest.mark.asyncio
    async def test_release_runs_when_stop_os_resource_raises(self) -> None:
        registry = ExecutionContextRegistry()
        factory = IsolatedExecutionContextFactory(registry=registry)

        context_id = _make_context_id()

        # Open a physical surface (no OS process started, so _start_os_resource
        # returns None). This is the simplest path to get a claimed surface.
        ctx = await factory.open(
            context_id=context_id,
            surface_kind=InputSurfaceKind.KEYBOARD,
            isolation_seed="seat0",
        )

        # Verify surface IS claimed after open.
        assert registry.owner_of(surface=ctx.surface) == context_id

        # Patch _stop_os_resource to raise so we exercise the finally branch.
        boom = RuntimeError("simulated OS resource teardown failure")
        with patch.object(
            factory, "_stop_os_resource", new=AsyncMock(side_effect=boom)
        ):
            with pytest.raises(RuntimeError, match="simulated OS resource teardown"):
                await factory.close(context_id=context_id)

        # Despite the raise, release_all_for must have run: surface is now free.
        assert registry.owner_of(surface=ctx.surface) is None

    @pytest.mark.asyncio
    async def test_release_runs_on_clean_close(self) -> None:
        """Sanity: normal path also releases (no regression)."""
        registry = ExecutionContextRegistry()
        factory = IsolatedExecutionContextFactory(registry=registry)

        context_id = _make_context_id()
        ctx = await factory.open(
            context_id=context_id,
            surface_kind=InputSurfaceKind.MOUSE,
            isolation_seed="seat0",
        )

        assert registry.owner_of(surface=ctx.surface) == context_id
        await factory.close(context_id=context_id)
        assert registry.owner_of(surface=ctx.surface) is None

    @pytest.mark.asyncio
    async def test_close_unknown_context_is_noop(self) -> None:
        """close() with an unknown context_id does not raise."""
        registry = ExecutionContextRegistry()
        factory = IsolatedExecutionContextFactory(registry=registry)
        unknown = _make_context_id()
        # Should not raise.
        await factory.close(context_id=unknown)
