"""Unit tests — IsolatedExecutionContextFactory + BrowserAdmissionGuard (Phase 2a).

Verifies the acquire/release invariants for each surface kind:
- BROWSER: open acquires permit, close releases permit.
- KEYBOARD/MOUSE/SCREEN (physical): no permit acquired or released.
- Exception in _start_os_resource after acquire → permit released (no orphan).
- close after open-failure rollback does not double-release.

These tests complement the existing test_isolated_execution_context_factory.py
which covers the registry/surface-orphan invariants without the guard.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from hermes.execution.application.browser_admission_guard import BrowserAdmissionGuard
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NullMemReader:
    """Always returns a large value — dynamic guard never parks."""

    def mem_available_mb(self) -> int | None:
        return 100_000


def _make_context_id() -> ExecutionContextId:
    return ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.AGENT_TASK)


def _make_guard(capacity: int = 10) -> BrowserAdmissionGuard:
    """Make a guard with a fixed large-memory reader and explicit max_sessions."""
    import os
    with patch.dict(os.environ, {
        "HERMES_BROWSER_MAX_SESSIONS": str(capacity),
        "HERMES_BROWSER_HEADROOM_MB": "0",
    }, clear=False):
        return BrowserAdmissionGuard(memory_reader=_NullMemReader())


# ---------------------------------------------------------------------------
# BROWSER surface: acquire on open, release on close
# ---------------------------------------------------------------------------


class TestBrowserAcquiresPermit:
    @pytest.mark.asyncio
    async def test_browser_open_acquires_permit(self) -> None:
        registry = ExecutionContextRegistry()
        guard = _make_guard()
        factory = IsolatedExecutionContextFactory(registry=registry, guard=guard)

        context_id = _make_context_id()
        assert guard.active_sessions() == 0

        await factory.open(
            context_id=context_id,
            surface_kind=InputSurfaceKind.BROWSER,
            isolation_seed="session-1",
        )
        assert guard.active_sessions() == 1

    @pytest.mark.asyncio
    async def test_browser_close_releases_permit(self) -> None:
        registry = ExecutionContextRegistry()
        guard = _make_guard()
        factory = IsolatedExecutionContextFactory(registry=registry, guard=guard)

        context_id = _make_context_id()
        await factory.open(
            context_id=context_id,
            surface_kind=InputSurfaceKind.BROWSER,
            isolation_seed="session-2",
        )
        assert guard.active_sessions() == 1

        await factory.close(context_id=context_id)
        assert guard.active_sessions() == 0

    @pytest.mark.asyncio
    async def test_close_releases_even_if_stop_os_resource_raises(self) -> None:
        """Registry + guard both release even on _stop_os_resource failure."""
        registry = ExecutionContextRegistry()
        guard = _make_guard()
        factory = IsolatedExecutionContextFactory(registry=registry, guard=guard)

        context_id = _make_context_id()
        ctx = await factory.open(
            context_id=context_id,
            surface_kind=InputSurfaceKind.BROWSER,
            isolation_seed="session-fail",
        )
        assert guard.active_sessions() == 1
        assert registry.owner_of(surface=ctx.surface) == context_id

        boom = RuntimeError("simulated stop failure")
        with patch.object(factory, "_stop_os_resource", new=AsyncMock(side_effect=boom)):
            with pytest.raises(RuntimeError, match="simulated stop failure"):
                await factory.close(context_id=context_id)

        # Both guard and registry must be clean after the failure
        assert guard.active_sessions() == 0
        assert registry.owner_of(surface=ctx.surface) is None


# ---------------------------------------------------------------------------
# Physical surfaces (KEYBOARD, MOUSE, SCREEN): no permit
# ---------------------------------------------------------------------------


class TestPhysicalSurfaceNeverAcquires:
    @pytest.mark.asyncio
    async def test_keyboard_open_does_not_acquire(self) -> None:
        registry = ExecutionContextRegistry()
        guard = _make_guard()
        factory = IsolatedExecutionContextFactory(registry=registry, guard=guard)

        context_id = _make_context_id()
        await factory.open(
            context_id=context_id,
            surface_kind=InputSurfaceKind.KEYBOARD,
            isolation_seed="seat0",
        )
        assert guard.active_sessions() == 0

    @pytest.mark.asyncio
    async def test_keyboard_close_does_not_release_guard(self) -> None:
        registry = ExecutionContextRegistry()
        guard = _make_guard()
        factory = IsolatedExecutionContextFactory(registry=registry, guard=guard)

        context_id = _make_context_id()
        await factory.open(
            context_id=context_id,
            surface_kind=InputSurfaceKind.KEYBOARD,
            isolation_seed="seat0",
        )
        await factory.close(context_id=context_id)
        # Guard unaffected — no double release
        assert guard.active_sessions() == 0

    @pytest.mark.asyncio
    async def test_mouse_surface_no_permit(self) -> None:
        registry = ExecutionContextRegistry()
        guard = _make_guard()
        factory = IsolatedExecutionContextFactory(registry=registry, guard=guard)

        context_id = _make_context_id()
        await factory.open(
            context_id=context_id,
            surface_kind=InputSurfaceKind.MOUSE,
            isolation_seed="seat0",
        )
        assert guard.active_sessions() == 0
        await factory.close(context_id=context_id)
        assert guard.active_sessions() == 0


# ---------------------------------------------------------------------------
# Orphan permit regression: failure in _start_os_resource after acquire
# ---------------------------------------------------------------------------


class TestOrphanPermitRegression:
    @pytest.mark.asyncio
    async def test_permit_released_when_start_os_resource_raises(self) -> None:
        """If _start_os_resource raises after the guard permit is acquired,
        the permit must be released (no orphan) and the registry claim reverted."""
        registry = ExecutionContextRegistry()
        guard = _make_guard(capacity=2)
        factory = IsolatedExecutionContextFactory(registry=registry, guard=guard)

        context_id = _make_context_id()

        boom = RuntimeError("simulated spawn failure")
        with patch.object(
            factory, "_start_os_resource", new=AsyncMock(side_effect=boom)
        ):
            with pytest.raises(RuntimeError, match="simulated spawn failure"):
                await factory.open(
                    context_id=context_id,
                    surface_kind=InputSurfaceKind.BROWSER,
                    isolation_seed="session-boom",
                )

        # Guard permit fully restored
        assert guard.active_sessions() == 0

        # Registry claim reverted (surface free)
        from hermes.execution.domain.ports import InputSurfaceKey  # noqa: PLC0415
        surface = InputSurfaceKey(
            kind=InputSurfaceKind.BROWSER, surface_id="session-boom"
        )
        assert registry.owner_of(surface=surface) is None

        # Can open new contexts at full capacity
        ctx2_id = _make_context_id()
        await factory.open(
            context_id=ctx2_id,
            surface_kind=InputSurfaceKind.BROWSER,
            isolation_seed="session-after",
        )
        assert guard.active_sessions() == 1
        await factory.close(context_id=ctx2_id)
        assert guard.active_sessions() == 0


# ---------------------------------------------------------------------------
# No guard: behavior unchanged (backwards compat)
# ---------------------------------------------------------------------------


class TestNoGuardBackwardsCompat:
    @pytest.mark.asyncio
    async def test_browser_open_without_guard_works(self) -> None:
        """With guard=None, BROWSER surfaces open and close without errors."""
        registry = ExecutionContextRegistry()
        factory = IsolatedExecutionContextFactory(registry=registry, guard=None)

        context_id = _make_context_id()
        ctx = await factory.open(
            context_id=context_id,
            surface_kind=InputSurfaceKind.BROWSER,
            isolation_seed="no-guard",
        )
        assert registry.owner_of(surface=ctx.surface) == context_id
        await factory.close(context_id=context_id)
        assert registry.owner_of(surface=ctx.surface) is None
