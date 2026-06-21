"""T058 — Reconciliación de huérfanos tras reinicio (SC-010, CTRL-P1-20).

Cubre:
  SC-010: reinicio del daemon con M tareas en curso → reconcile_orphans purga
          dueños huérfanos → 0 huérfanos tras bootstrap.
  FR-026: al arrancar el daemon, ninguna superficie física queda marcada por
          un dueño muerto.
  FR-023: release en finally no filtra superficies (cleanup-safe, no-op doble).
  CTRL-P1-20: lease vencido → liberado por reconcile_orphans.
"""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.execution.application.execution_context_registry import (
    ExecutionContextRegistry,
)
from hermes.execution.domain.ports import (
    ExecutionContextId,
    InputOwnerKind,
    InputSurfaceKey,
    InputSurfaceKind,
)
from hermes.execution.infrastructure.sqlite_execution_context_store import (
    SqliteExecutionContextStore,
)

pytestmark = pytest.mark.integration

_SHORT_LEASE_S = 0.05  # 50 ms — expira rápido en tests


def _agent(uid=None) -> ExecutionContextId:
    return ExecutionContextId(value=uid or uuid4(), owner_kind=InputOwnerKind.AGENT_TASK)


def _surface(idx: int = 0) -> InputSurfaceKey:
    return InputSurfaceKey(kind=InputSurfaceKind.BROWSER, surface_id=f"exec:tenant-{idx}:site-main")


def _isolation_key(idx: int = 0) -> str:
    return f"exec:tenant-{idx}:site-main"


# ---------------------------------------------------------------------------
# T058-A: reconcile clears all in-memory owners (FR-026)
# ---------------------------------------------------------------------------


class TestReconcileInMemory:
    def test_reconcile_purges_all_in_memory_owners(self) -> None:
        """Tras reinicio, el registry arranca vacío. reconcile() = no-op de limpieza."""
        reg = ExecutionContextRegistry()
        s1, s2, s3 = _surface(0), _surface(1), _surface(2)
        reg.claim(surface=s1, owner=_agent(), isolation_key=_isolation_key(0))
        reg.claim(surface=s2, owner=_agent(), isolation_key=_isolation_key(1))
        reg.claim(surface=s3, owner=_agent(), isolation_key=_isolation_key(2))

        purged = reg.reconcile()

        assert purged == 3
        assert reg.owner_of(surface=s1) is None
        assert reg.owner_of(surface=s2) is None
        assert reg.owner_of(surface=s3) is None

    def test_reconcile_on_empty_registry_returns_zero(self) -> None:
        reg = ExecutionContextRegistry()
        assert reg.reconcile() == 0

    def test_surfaces_claimable_after_reconcile(self) -> None:
        reg = ExecutionContextRegistry()
        surface = _surface(0)
        owner_a = _agent()

        reg.claim(surface=surface, owner=owner_a, isolation_key=_isolation_key(0))
        reg.reconcile()

        owner_b = _agent()
        reg.claim(surface=surface, owner=owner_b, isolation_key=_isolation_key(0))
        assert reg.owner_of(surface=surface) == owner_b


# ---------------------------------------------------------------------------
# T058-B: simulate daemon restart — orphan purge via SQLite (SC-010)
# ---------------------------------------------------------------------------


class TestReconcileOrphansViaSQLite:
    def test_orphans_purged_on_restart(self, tmp_path: Path) -> None:
        """El 'primer daemon' reclama M superficies y muere sin release.
        El 'segundo daemon' crea un nuevo registry sobre la MISMA DB y llama
        reconcile_orphans → todas las claimed quedan released."""
        db_path = tmp_path / "shell-state.db"
        m = 3

        # --- primer daemon (muere sin release) ---
        store_1 = SqliteExecutionContextStore(db_path=db_path)
        reg_1 = ExecutionContextRegistry(store=store_1)
        for i in range(m):
            reg_1.claim(
                surface=_surface(i),
                owner=_agent(),
                isolation_key=_isolation_key(i),
            )
        # 'daemon cae' — no hace release

        # --- segundo daemon (bootstrap) ---
        store_2 = SqliteExecutionContextStore(db_path=db_path)
        purged = store_2.reconcile_orphans()

        assert purged == m, f"Expected {m} orphans purged, got {purged}"
        assert store_2.list_claimed() == []

    def test_released_rows_not_counted_as_orphans(self, tmp_path: Path) -> None:
        """Una superficie correctamente liberada no cuenta como huérfano."""
        db_path = tmp_path / "shell-state.db"
        store = SqliteExecutionContextStore(db_path=db_path)
        reg = ExecutionContextRegistry(store=store)
        surface = _surface(0)

        reg.claim(surface=surface, owner=_agent(), isolation_key=_isolation_key(0))
        reg.release(surface=surface)

        # Mismo store, reconcile
        purged = store.reconcile_orphans()
        assert purged == 0


# ---------------------------------------------------------------------------
# T058-C: lease vencido → liberado en reconcile_orphans (CTRL-P1-20)
# ---------------------------------------------------------------------------


class TestLeaseExpiry:
    def test_expired_lease_is_purged_by_reconcile(self, tmp_path: Path) -> None:
        """Contextos con lease_expires_at en el pasado son liberados.

        reconcile_orphans en bootstrap libera TODOS los claimed porque el
        proceso que los tenía ha muerto — el lease del proceso anterior es
        irrelevante (FR-026: al arrancar, ninguna superficie queda marcada por
        un dueño muerto).
        """
        db_path = tmp_path / "shell-state.db"
        store = SqliteExecutionContextStore(db_path=db_path)
        reg = ExecutionContextRegistry(store=store)

        # Claim con lease muy corto
        surface = _surface(0)
        reg.claim(
            surface=surface,
            owner=_agent(),
            isolation_key=_isolation_key(0),
            lease_seconds=_SHORT_LEASE_S,
        )

        # Esperar expiración
        time.sleep(_SHORT_LEASE_S * 3)

        # reconcile_orphans libera todo (boot scenario)
        purged = store.reconcile_orphans()
        assert purged >= 1
        assert store.list_claimed() == []

    def test_all_claimed_purged_on_restart_regardless_of_lease(
        self, tmp_path: Path
    ) -> None:
        """FR-026: en el restart, incluso un lease largo se purga.

        El proceso que tenía el lease ha muerto. La segunda instancia del daemon
        no puede asumir que algún process está vivo. reconcile_orphans libera
        todos los claimed.
        """
        db_path = tmp_path / "shell-state.db"

        # Primer daemon — claim con lease largo
        store_first = SqliteExecutionContextStore(db_path=db_path)
        reg_first = ExecutionContextRegistry(store=store_first)
        reg_first.claim(
            surface=_surface(0),
            owner=_agent(),
            isolation_key=_isolation_key(0),
            lease_seconds=3600.0,  # 1 hora — pero el proceso muere
        )

        # Segundo daemon bootstrap: reconcile libera todo
        store_second = SqliteExecutionContextStore(db_path=db_path)
        purged = store_second.reconcile_orphans()
        assert purged == 1
        assert store_second.list_claimed() == []


# ---------------------------------------------------------------------------
# T058-D: release in finally — no leak, no double-release error
# ---------------------------------------------------------------------------


class TestFinallyRelease:
    def test_release_in_finally_no_leak(self, tmp_path: Path) -> None:
        """release() en finally es cleanup-safe: no-op si ya libre."""
        db_path = tmp_path / "shell-state.db"
        store = SqliteExecutionContextStore(db_path=db_path)
        reg = ExecutionContextRegistry(store=store)
        surface = _surface(0)
        owner = _agent()

        try:
            reg.claim(surface=surface, owner=owner, isolation_key=_isolation_key(0))
            # simular excepción del worker
            raise RuntimeError("worker failed")
        except RuntimeError:
            pass
        finally:
            reg.release(surface=surface)  # no debe lanzar

        assert reg.owner_of(surface=surface) is None
        assert store.list_claimed() == []

    def test_double_release_is_noop(self, tmp_path: Path) -> None:
        """Doble release no lanza (cleanup-safe, FR-023)."""
        db_path = tmp_path / "shell-state.db"
        store = SqliteExecutionContextStore(db_path=db_path)
        reg = ExecutionContextRegistry(store=store)
        surface = _surface(0)

        reg.claim(surface=surface, owner=_agent(), isolation_key=_isolation_key(0))
        reg.release(surface=surface)
        reg.release(surface=surface)  # no debe lanzar


# ---------------------------------------------------------------------------
# T058-E: M simultaneous tasks on restart → 0 orphans after reconcile
# ---------------------------------------------------------------------------


class TestMTasksRestartZeroOrphans:
    def test_m_tasks_in_progress_zero_orphans_after_restart(self, tmp_path: Path) -> None:
        """Escenario completo: daemon muere con M=4 tareas en curso (sin release).
        El bootstrap del segundo daemon llama reconcile_orphans → 0 huérfanos."""
        db_path = tmp_path / "shell-state.db"
        m = 4

        # Primer daemon — simula M tareas en curso (sin terminar)
        store_first = SqliteExecutionContextStore(db_path=db_path)
        reg_first = ExecutionContextRegistry(store=store_first)
        for i in range(m):
            reg_first.claim(
                surface=_surface(i),
                owner=_agent(),
                isolation_key=_isolation_key(i),
            )

        remaining_before = store_first.list_claimed()
        assert len(remaining_before) == m

        # Segundo daemon bootstrap: nuevo store + reconcile
        store_second = SqliteExecutionContextStore(db_path=db_path)
        purged = store_second.reconcile_orphans()

        assert purged == m
        # SC-010: 0 huérfanos
        assert store_second.list_claimed() == []
