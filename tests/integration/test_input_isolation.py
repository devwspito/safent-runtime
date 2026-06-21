"""T057 — Aislamiento de input: UN dueño por superficie, fail-closed (SC-007).

Cubre:
  SC-007: N>=4 contextos concurrentes compitiendo por superficies → 0 colisiones.
  FR-022: segundo claim de la misma (surface, isolation_key) por otro owner →
          InputOwnershipViolation.
  OQ-3:   mapeo superficie→context_id inyectivo (misma física=mismo key;
          distintas físicas nunca colisionan).
  CTRL-P1-18/19 (threat-model §3.4).

No toca BrowserPort / StorageStatePort (Constitución I / FR-028).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hermes.execution.application.execution_context_registry import (
    ExecutionContextRegistry,
)
from hermes.execution.domain.isolation_key_mapper import (
    IsolationKeyMapper,
    PhysicalSurface,
)
from hermes.execution.domain.ports import (
    ExecutionContextId,
    InputOwnerKind,
    InputSurfaceKey,
    InputSurfaceKind,
    InputOwnershipViolation,
)
from hermes.execution.infrastructure.sqlite_execution_context_store import (
    SqliteExecutionContextStore,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_owner(uid: UUID | None = None) -> ExecutionContextId:
    return ExecutionContextId(
        value=uid or uuid4(),
        owner_kind=InputOwnerKind.AGENT_TASK,
    )


def _operator_owner(uid: UUID | None = None) -> ExecutionContextId:
    return ExecutionContextId(
        value=uid or uuid4(),
        owner_kind=InputOwnerKind.OPERATOR,
    )


def _browser_surface(session: str) -> InputSurfaceKey:
    return InputSurfaceKey(kind=InputSurfaceKind.BROWSER, surface_id=session)


def _kb_surface(seat: str = "seat0") -> InputSurfaceKey:
    return InputSurfaceKey(kind=InputSurfaceKind.KEYBOARD, surface_id=seat)


# ---------------------------------------------------------------------------
# T057-A: claim / fail-closed / idempotency (pure in-memory registry)
# ---------------------------------------------------------------------------


class TestRegistryClaim:
    def test_fresh_surface_claim_succeeds(self) -> None:
        reg = ExecutionContextRegistry()
        surface = _browser_surface("exec:tenant-1:site-a")
        owner = _agent_owner()

        reg.claim(surface=surface, owner=owner)

        assert reg.owner_of(surface=surface) == owner

    def test_second_claim_different_owner_raises(self) -> None:
        """FR-022: fail-closed, negación deja traza en el caller."""
        reg = ExecutionContextRegistry()
        surface = _browser_surface("exec:tenant-1:site-b")
        owner_a = _agent_owner()
        owner_b = _agent_owner()

        reg.claim(surface=surface, owner=owner_a)

        with pytest.raises(InputOwnershipViolation):
            reg.claim(surface=surface, owner=owner_b)

    def test_same_owner_claim_twice_is_idempotent(self) -> None:
        reg = ExecutionContextRegistry()
        surface = _browser_surface("exec:tenant-1:site-c")
        owner = _agent_owner()

        reg.claim(surface=surface, owner=owner)
        reg.claim(surface=surface, owner=owner)  # must not raise

        assert reg.owner_of(surface=surface) == owner

    def test_release_frees_surface(self) -> None:
        reg = ExecutionContextRegistry()
        surface = _kb_surface()
        owner = _operator_owner()

        reg.claim(surface=surface, owner=owner)
        reg.release(surface=surface)

        assert reg.owner_of(surface=surface) is None

    def test_release_unknown_surface_is_noop(self) -> None:
        reg = ExecutionContextRegistry()
        reg.release(surface=_browser_surface("ghost"))  # must not raise

    def test_re_claim_after_release_succeeds(self) -> None:
        reg = ExecutionContextRegistry()
        surface = _kb_surface()
        owner_a = _agent_owner()
        owner_b = _operator_owner()

        reg.claim(surface=surface, owner=owner_a)
        reg.release(surface=surface)
        reg.claim(surface=surface, owner=owner_b)

        assert reg.owner_of(surface=surface) == owner_b

    def test_free_surface_returns_none(self) -> None:
        reg = ExecutionContextRegistry()
        assert reg.owner_of(surface=_browser_surface("no-one")) is None

    def test_operator_vs_agent_task_different_owners(self) -> None:
        reg = ExecutionContextRegistry()
        surface = _kb_surface()
        agent = _agent_owner()

        reg.claim(surface=surface, owner=agent)

        with pytest.raises(InputOwnershipViolation):
            reg.claim(surface=surface, owner=_operator_owner())


# ---------------------------------------------------------------------------
# T057-B: release_all_for — worker termination without leaks
# ---------------------------------------------------------------------------


class TestReleaseAllFor:
    def test_releases_all_surfaces_for_owner(self) -> None:
        reg = ExecutionContextRegistry()
        owner = _agent_owner()
        s1 = _browser_surface("exec:t1:s1")
        s2 = _browser_surface("exec:t1:s2")
        s3 = _kb_surface("seat0")

        reg.claim(surface=s1, owner=owner)
        reg.claim(surface=s2, owner=owner)
        reg.claim(surface=s3, owner=owner)

        released = reg.release_all_for(owner=owner)

        assert released == 3
        assert reg.owner_of(surface=s1) is None
        assert reg.owner_of(surface=s2) is None
        assert reg.owner_of(surface=s3) is None

    def test_release_all_does_not_touch_other_owners(self) -> None:
        reg = ExecutionContextRegistry()
        owner_a = _agent_owner()
        owner_b = _agent_owner()
        s_a = _browser_surface("exec:tenant-a:site-x")
        s_b = _browser_surface("exec:tenant-b:site-y")

        reg.claim(surface=s_a, owner=owner_a)
        reg.claim(surface=s_b, owner=owner_b)

        reg.release_all_for(owner=owner_a)

        assert reg.owner_of(surface=s_a) is None
        assert reg.owner_of(surface=s_b) == owner_b

    def test_release_all_for_unknown_owner_returns_zero(self) -> None:
        reg = ExecutionContextRegistry()
        released = reg.release_all_for(owner=_agent_owner())
        assert released == 0


# ---------------------------------------------------------------------------
# T057-C: N>=4 concurrent claims → 0 collisions (SC-007)
# ---------------------------------------------------------------------------


class TestConcurrentClaims:
    async def test_four_workers_distinct_surfaces_no_collision(self) -> None:
        """SC-007: 4 workers, cada uno su propia superficie → 0 colisiones."""
        reg = ExecutionContextRegistry()
        n_workers = 4
        errors: list[Exception] = []

        async def worker(idx: int) -> None:
            surface = _browser_surface(f"exec:tenant-{idx}:site-main")
            owner = _agent_owner()
            try:
                reg.claim(surface=surface, owner=owner)
                await asyncio.sleep(0)  # yield
                assert reg.owner_of(surface=surface) == owner
                reg.release(surface=surface)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        await asyncio.gather(*[worker(i) for i in range(n_workers)])
        assert errors == [], f"Collision errors: {errors}"

    async def test_four_workers_same_surface_only_one_wins(self) -> None:
        """Cuatro workers compitiendo por la MISMA superficie → exactamente 1 gana."""
        reg = ExecutionContextRegistry()
        surface = _kb_surface("seat0")
        winners: list[ExecutionContextId] = []
        violations: list[InputOwnershipViolation] = []

        async def worker() -> None:
            owner = _agent_owner()
            try:
                reg.claim(surface=surface, owner=owner)
                winners.append(owner)
            except InputOwnershipViolation as exc:
                violations.append(exc)

        await asyncio.gather(*[worker() for _ in range(4)])

        assert len(winners) == 1, f"Expected 1 winner, got {len(winners)}"
        assert len(violations) == 3
        # El ganador sigue siendo dueño
        assert reg.owner_of(surface=surface) == winners[0]


# ---------------------------------------------------------------------------
# T057-D: mapeo inyectivo superficie-física → context_id (OQ-3, CTRL-P1-18)
# ---------------------------------------------------------------------------


class TestInjectionKeyMapper:
    """Misma superficie física → mismo key. Distintas físicas → no colisionan."""

    def test_same_physical_surface_always_same_key(self) -> None:
        key_a = IsolationKeyMapper.key_for(
            PhysicalSurface(kind=InputSurfaceKind.BROWSER, surface_id="exec:t1:s1")
        )
        key_b = IsolationKeyMapper.key_for(
            PhysicalSurface(kind=InputSurfaceKind.BROWSER, surface_id="exec:t1:s1")
        )
        assert key_a == key_b

    def test_distinct_physical_surfaces_never_collide(self) -> None:
        surfaces = [
            PhysicalSurface(kind=InputSurfaceKind.BROWSER, surface_id=f"exec:tenant-{i}:site-main")
            for i in range(8)
        ]
        keys = [IsolationKeyMapper.key_for(s) for s in surfaces]
        assert len(keys) == len(set(keys)), "Injective violation: two surfaces share the same key"

    def test_keyboard_primary_seat_produces_stable_key(self) -> None:
        key1 = IsolationKeyMapper.key_for(
            PhysicalSurface(kind=InputSurfaceKind.KEYBOARD, surface_id="seat0")
        )
        key2 = IsolationKeyMapper.key_for(
            PhysicalSurface(kind=InputSurfaceKind.KEYBOARD, surface_id="seat0")
        )
        assert key1 == key2

    def test_keyboard_and_mouse_same_seat_distinct_keys(self) -> None:
        kb = IsolationKeyMapper.key_for(
            PhysicalSurface(kind=InputSurfaceKind.KEYBOARD, surface_id="seat0")
        )
        mouse = IsolationKeyMapper.key_for(
            PhysicalSurface(kind=InputSurfaceKind.MOUSE, surface_id="seat0")
        )
        assert kb != mouse

    def test_browser_and_keyboard_distinct_keys(self) -> None:
        browser = IsolationKeyMapper.key_for(
            PhysicalSurface(kind=InputSurfaceKind.BROWSER, surface_id="exec:t1:s1")
        )
        keyboard = IsolationKeyMapper.key_for(
            PhysicalSurface(kind=InputSurfaceKind.KEYBOARD, surface_id="exec:t1:s1")
        )
        assert browser != keyboard

    def test_mapper_builds_surface_key_correctly(self) -> None:
        physical = PhysicalSurface(kind=InputSurfaceKind.SCREEN, surface_id=":99")
        surface_key = IsolationKeyMapper.surface_key_for(physical)
        assert surface_key.kind == InputSurfaceKind.SCREEN
        assert surface_key.surface_id == ":99"


# ---------------------------------------------------------------------------
# T057-E: registry + SQLite write-through (integration with store)
# ---------------------------------------------------------------------------


class TestRegistryWithStore:
    def test_claim_persisted_to_sqlite(self, tmp_path: Path) -> None:
        store = SqliteExecutionContextStore(db_path=tmp_path / "state.db")
        reg = ExecutionContextRegistry(store=store)
        surface = _browser_surface("exec:t1:site-a")
        owner = _agent_owner()
        isolation_key = "exec:tenant-1:site-a"

        reg.claim(surface=surface, owner=owner, isolation_key=isolation_key)

        rows = store.list_claimed()
        assert len(rows) == 1
        assert rows[0]["isolation_key"] == isolation_key

    def test_release_marks_released_in_sqlite(self, tmp_path: Path) -> None:
        store = SqliteExecutionContextStore(db_path=tmp_path / "state.db")
        reg = ExecutionContextRegistry(store=store)
        surface = _browser_surface("exec:t1:site-b")
        owner = _agent_owner()

        reg.claim(surface=surface, owner=owner, isolation_key="exec:t1:site-b")
        reg.release(surface=surface)

        rows = store.list_claimed()
        assert rows == []
