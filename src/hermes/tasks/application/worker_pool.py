"""T066/T067/T069 — WorkerPool: N workers concurrentes (FR-020/FR-024/FR-025).

Refactorización de AgentLoopOrchestrator.run_forever (mono-worker) a un pool
de N corrutinas asyncio que drenan la WorkQueue concurrentemente.

DISEÑO (feature 006 / PIEZA 4 / worker_pool_port.py):
  - N corrutinas _worker_loop(): cada una ejecuta el ciclo claim->_process->mark
    INTACTO del AgentLoopOrchestrator (reutilizado por delegación, no duplicado).
  - WorkerWakeSignal: asyncio.Condition — wake_one despierta UN worker libre;
    wake_all los despierta todos (p. ej. al tomar varias tareas de golpe).
  - Watchdog DEDICADO (T067/NFR-006): corrutina independiente que emite
    sd_notify WATCHDOG=1 sin depender de los workers — una tarea larga NO
    detiene el latido.
  - Broker INSTANCIA ÚNICA (T068/CTRL-P1-21): inyectado en el constructor,
    compartido por todos los workers. is_paused() NO cacheado per-worker
    (estado en AgentStatePort, leído en cada vuelta y en cada dispatch).
  - ExecutionContextRegistry (T069): se reconcilia en bootstrap(); cada worker
    que toca una superficie la reclama (fail-closed) y libera en finally.

Tamaño del pool:
  - HERMES_WORKER_POOL_SIZE env var: default 1 en prod (hasta SC-007 verde),
    default 4 en test (parametrizable en run_forever(size=N)).

Concurrencia de la cola:
  - La cola SQLite ya garantiza claim atómico (BEGIN IMMEDIATE). N workers
    compiten de forma segura sin doble-toma.

API pública del orchestrator INTACTA:
  - bootstrap()/run_forever()/request_shutdown() — los tests de P0 siguen verdes.
  - wake_signal property delegado al pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
from collections.abc import Callable
from typing import Any

from hermes.capabilities.domain.ports import (
    CapabilityBrokerPort,
    ConsentContext,
)
from hermes.tasks.domain.ports import AgentStatePort, WorkQueuePort

logger = logging.getLogger("hermes.tasks.pool")

# Intervalo del watchdog dedicado (emite WATCHDOG=1 aunque un worker esté bloqueado).
_WATCHDOG_INTERVAL_S = float(os.environ.get("HERMES_WATCHDOG_INTERVAL_S", "5.0"))

# Defaults de sizing (las env vars los sobreescriben EN CADA LLAMADA, ver
# _resolve_worker_pool_size). Son literales, no lecturas de env en import.
# Memoria estimada por worker en MB (LLM cycle + API calls en vuelo).
_WORKER_OVERHEAD_MB = 16

# Máximo absoluto de workers por defecto, independiente de RAM.
_WORKER_POOL_HARD_CAP = 256

# Safe default cuando /proc/meminfo no es legible.
_MEMINFO_FALLBACK_WORKERS = 8


def _resolve_worker_pool_size() -> int:
    """Calcula el tamaño óptimo del pool — única fuente de verdad de sizing.

    Prioridad:
    1. HERMES_WORKER_POOL_SIZE env var → uso directo (operador lo fijó).
    2. RAM disponible: floor(MemAvailable_MB / HERMES_WORKER_OVERHEAD_MB).
       Clampeado a [1, HERMES_WORKER_POOL_HARD_CAP].

    Las env vars se leen EN CADA LLAMADA (no constantes de import), para que el
    hard cap / overhead sean configurables en runtime y testeables en aislamiento.

    /proc/meminfo puede no existir fuera de Linux — en ese caso se aplica
    _MEMINFO_FALLBACK_WORKERS (safe, no falla-abierto a un número grande).
    """
    hard_cap = int(os.environ.get("HERMES_WORKER_POOL_HARD_CAP", str(_WORKER_POOL_HARD_CAP)))
    overhead_mb = int(os.environ.get("HERMES_WORKER_OVERHEAD_MB", str(_WORKER_OVERHEAD_MB)))

    explicit = os.environ.get("HERMES_WORKER_POOL_SIZE")
    if explicit is not None:
        return max(1, min(int(explicit), hard_cap))

    mem_available_mb = _read_mem_available_mb()
    if mem_available_mb is None:
        logger.warning(
            "hermes.tasks.pool.meminfo_unreadable — "
            "falling back to %d workers",
            _MEMINFO_FALLBACK_WORKERS,
        )
        return _MEMINFO_FALLBACK_WORKERS

    raw = math.floor(mem_available_mb / overhead_mb)
    return max(1, min(raw, hard_cap))


def _read_mem_available_mb() -> int | None:
    """Lee MemAvailable de /proc/meminfo. Retorna None si no es legible."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    # Formato: "MemAvailable:   12345678 kB"
                    kb = int(line.split()[1])
                    return kb // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


class WorkerPool:
    """Pool de N workers asyncio que drenan la cola concurrentemente.

    Implementa WorkerPoolPort (ver worker_pool_port.py). Reutiliza el _process
    del AgentLoopOrchestrator vía delegación — no duplica la lógica de dispatch.

    Inyección de dependencias idéntica a AgentLoopOrchestrator para que el
    orchestrator pueda delegar run_forever() en el pool sin cambiar el cableado.
    """

    def __init__(
        self,
        *,
        queue: WorkQueuePort,
        state: AgentStatePort,
        engine: Any,
        broker: CapabilityBrokerPort,
        consent_context: ConsentContext,
        notify_watchdog: Callable[[], None],
        idle_poll_s: float = 1.0,
        pause_poll_s: float = 5.0,
        firmer: Any | None = None,
        audit_repo: Any | None = None,
        approval_gate: Any | None = None,
        intent_log: Any | None = None,
        chunk_sink: Any | None = None,
        execution_registry: Any | None = None,  # ExecutionContextRegistryPort | None
        browser_adapter: Any | None = None,  # BrowserSurfaceAdapter | None
        agent_registry: Any | None = None,  # AgentRegistryPort | None (autonomy_level)
        conversation_repo: Any | None = None,  # SQLiteConversationRepository | None (Bug #2)
    ) -> None:
        self._queue = queue
        self._state = state
        self._engine = engine
        self._broker = broker  # INSTANCIA ÚNICA: compartida por todos los workers (CTRL-P1-21)
        self._consent = consent_context
        self._notify_watchdog = notify_watchdog
        self._idle_poll_s = idle_poll_s
        self._pause_poll_s = pause_poll_s
        self._firmer = firmer
        self._audit_repo = audit_repo
        self._approval_gate = approval_gate
        self._intent_log = intent_log
        self._chunk_sink = chunk_sink
        self._registry = execution_registry
        self._browser_adapter = browser_adapter
        self._agent_registry = agent_registry
        self._conversation_repo = conversation_repo  # SQLiteConversationRepository | None

        self._shutdown = asyncio.Event()
        # Condition para wake-on-enqueue con N workers (CTRL-P1-12).
        self._wake_condition: asyncio.Condition = asyncio.Condition()
        self._wake_signal = _PoolWakeSignal(self._wake_condition)

        self._active_count = 0
        # Set by run_forever() once the actual size is resolved (env or RAM).
        self._configured_size: int | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def bootstrap(self) -> None:
        """Reconcilia huérfanos en la cola + el registry de ejecución (SC-010)."""
        n = await self._queue.reconcile_stale()
        if n > 0:
            logger.info(
                "hermes.tasks.pool.reconciled_stale",
                extra={"count": n},
            )
        if self._registry is not None:
            purged = self._registry.reconcile()
            if purged > 0:
                logger.info(
                    "hermes.tasks.pool.reconciled_orphan_contexts",
                    extra={"count": purged},
                )
        await self._seed_firmer()
        await self._reconcile_pending_intents()

    async def run_forever(self, *, size: int | None = None) -> None:
        """Arranca `size` workers + watchdog dedicado. Termina con request_shutdown().

        Si `size` es None, delega en _resolve_worker_pool_size() (env var o RAM).
        El watchdog corre INDEPENDIENTE de los workers (T067/NFR-006):
        una tarea larga en un worker NO detiene el latido.
        """
        resolved_size = size if size is not None else _resolve_worker_pool_size()
        self._configured_size = resolved_size
        workers = [
            asyncio.create_task(
                self._worker_loop(worker_id=i), name=f"worker-{i}"
            )
            for i in range(resolved_size)
        ]
        watchdog_task = asyncio.create_task(
            self._watchdog_loop(), name="watchdog"
        )

        logger.info("hermes.tasks.pool.started", extra={"size": resolved_size})

        await asyncio.gather(*workers, return_exceptions=True)

        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watchdog_task

        logger.info("hermes.tasks.pool.stopped")

    def request_shutdown(self) -> None:
        """Parada limpia: señaliza todos los workers a salir (SIGTERM del daemon)."""
        self._shutdown.set()
        # Despertar a todos los workers ociosos para que detecten shutdown.
        # schedule_soon is safe from sync context in same event loop.
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon(lambda: asyncio.ensure_future(self._wake_all_async()))
        except RuntimeError:
            pass  # No running event loop — no workers to wake

    def active_worker_count(self) -> int:
        """Número de workers activos (procesando un item) en este momento."""
        return self._active_count

    def configured_size(self) -> int | None:
        """Tamaño del pool configurado en la última llamada a run_forever().

        None si run_forever() aún no ha sido invocado (pool no iniciado).
        Útil para dashboards y observabilidad sin introducir métricas push.
        """
        return self._configured_size

    @property
    def wake_signal(self) -> _PoolWakeSignal:
        """Señal wake-on-enqueue compatible con MonoWorkerWakeSignal (CTRL-P1-12)."""
        return self._wake_signal

    # ------------------------------------------------------------------
    # Private: watchdog dedicado (T067 / NFR-006)
    # ------------------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        """Emite WATCHDOG=1 periódicamente, INDEPENDIENTE de los workers.

        T067: una tarea larga en un worker NO puede bloquear el latido.
        """
        while not self._shutdown.is_set():
            self._notify_watchdog()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown.wait()),
                    timeout=_WATCHDOG_INTERVAL_S,
                )

    # ------------------------------------------------------------------
    # Private: ciclo de cada worker (FR-020 / FR-024)
    # ------------------------------------------------------------------

    async def _worker_loop(self, *, worker_id: int) -> None:
        """Ciclo de un worker: claim -> _process -> repeat (fail-safe)."""
        logger.debug("hermes.tasks.pool.worker_started", extra={"worker_id": worker_id})
        try:
            await self._worker_main(worker_id=worker_id)
        finally:
            logger.debug("hermes.tasks.pool.worker_stopped", extra={"worker_id": worker_id})

    async def _worker_main(self, *, worker_id: int) -> None:  # noqa: ARG002
        while True:
            if self._shutdown.is_set():
                return

            # CTRL-P1-21: is_paused() leído NO cacheado per-worker.
            # Estado en AgentStatePort (SQLite/in-memory), no en memoria local.
            if await self._state.is_paused():
                await self._idle_with_wake(self._pause_poll_s)
                continue

            # F-08: un claim contendido (SQLITE_BUSY) NO debe matar al worker.
            # Con PRAGMA busy_timeout el lock espera; aun así, ante cualquier
            # error transitorio idle+retry en vez de dejar morir la corrutina
            # (gather return_exceptions=True encogería el pool en silencio).
            try:
                item = await self._queue.claim_next()
            except Exception:  # noqa: BLE001 — resiliencia del worker
                logger.warning(
                    "hermes.tasks.pool.claim_failed_retry",
                    extra={"worker_id": worker_id},
                    exc_info=True,
                )
                await self._idle_with_wake(self._idle_poll_s)
                continue
            if item is None:
                await self._idle_with_wake(self._idle_poll_s)
                continue

            self._active_count += 1
            try:
                await self._process(item)
            finally:
                self._active_count -= 1

    async def _idle_with_wake(self, seconds: float) -> None:
        """Espera interruptible: wake_one() o shutdown sale antes del timeout."""
        if seconds <= 0:
            return
        async with self._wake_condition:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake_condition.wait(), timeout=seconds
                )

    # ------------------------------------------------------------------
    # Private: procesado de item (reutiliza lógica del orchestrator P0)
    # ------------------------------------------------------------------

    async def _process(self, item: Any) -> None:
        """Procesa un item reclamado. Delega en AgentLoopOrchestrator._process.

        El broker NO se recrea — es la misma instancia inyectada (CTRL-P1-21).
        Un heartbeat renueva el lease periódicamente (lease/3) para que
        reconcile_stale() no re-encole una tarea de browser larga (Phase 2a).
        Si renew_lease() devuelve False, el worker perdió el lease — se aborta
        limpiamente sin marcar completed (el claim_token guard lo rechazaría
        de todos modos, pero evitar trabajo desperdiciado).
        """
        from hermes.tasks.application.agent_loop_orchestrator import (  # noqa: PLC0415
            AgentLoopOrchestrator,
        )

        # Construir un orchestrator ligero que comparte TODAS las dependencias,
        # especialmente el broker (instancia única — CTRL-P1-21).
        orch = AgentLoopOrchestrator(
            queue=self._queue,
            state=self._state,
            engine=self._engine,
            broker=self._broker,  # MISMO broker (singleton) — CTRL-P1-21
            consent_context=self._consent,
            notify_watchdog=lambda: None,  # watchdog gestionado por el pool
            idle_poll_s=self._idle_poll_s,
            pause_poll_s=self._pause_poll_s,
            firmer=self._firmer,
            audit_repo=self._audit_repo,
            approval_gate=self._approval_gate,
            intent_log=self._intent_log,
            chunk_sink=self._chunk_sink,
            agent_registry=self._agent_registry,
            conversation_repo=self._conversation_repo,
        )

        heartbeat = asyncio.create_task(
            self._lease_heartbeat(item),
            name=f"lease-heartbeat-{item.id}",
        )
        try:
            await orch._process(item)  # type: ignore[attr-defined]
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
            if self._browser_adapter is not None:
                with contextlib.suppress(Exception):
                    await self._browser_adapter.close_task(item.id)

    async def _lease_heartbeat(self, item: Any) -> None:
        """Renews item lease at lease/3 intervals until cancelled.

        If renew_lease returns False the worker no longer owns the lease —
        logs loudly and stops renewing (the claim_token guard will reject
        mark_completed; the _process caller has already returned by then).
        """
        from hermes.tasks.infrastructure.sqlite_work_queue import (  # noqa: PLC0415
            _LEASE_SECONDS as _DB_LEASE,
        )

        lease_s = _DB_LEASE
        interval_s = max(1.0, lease_s / 3)

        while True:
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                return

            if item.claim_token is None:
                return

            try:
                still_owner = await self._queue.renew_lease(
                    item.id, claim_token=item.claim_token
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hermes.tasks.pool.lease_heartbeat.renew_error "
                    "item_id=%s error=%s",
                    item.id,
                    exc,
                )
                continue

            if not still_owner:
                logger.error(
                    "hermes.tasks.pool.lease_heartbeat.lease_lost "
                    "item_id=%s claim_token=%s — "
                    "another worker reclaimed the task; stopping heartbeat. "
                    "mark_completed will be rejected by claim_token guard.",
                    item.id,
                    item.claim_token,
                )
                return

            logger.debug(
                "hermes.tasks.pool.lease_heartbeat.renewed item_id=%s", item.id
            )

    async def _wake_all_async(self) -> None:
        """Despierta a todos los workers ociosos."""
        async with self._wake_condition:
            self._wake_condition.notify_all()

    # ------------------------------------------------------------------
    # Private: bootstrap helpers
    # ------------------------------------------------------------------

    async def _seed_firmer(self) -> None:
        if self._firmer is None or self._audit_repo is None:
            return
        head = await self._audit_repo.head_hash_hex()
        if head is not None:
            object.__setattr__(self._firmer, "_last_hash", bytes.fromhex(head))

    async def _reconcile_pending_intents(self) -> None:
        if self._intent_log is None:
            return
        task_ids = self._intent_log.pending_task_ids()
        if not task_ids:
            return
        logger.warning(
            "hermes.tasks.pool.pending_intents_detected",
            extra={"count": len(task_ids)},
        )
        for task_id_str in task_ids:
            logger.error(
                "hermes.tasks.pool.needs_human_review: "
                "task_id=%s tiene intent sin outcome — posible efecto parcial.",
                task_id_str,
            )


# ---------------------------------------------------------------------------
# Pool-aware WakeSignal: asyncio.Condition despierta a UN worker libre (CTRL-P1-12)
# ---------------------------------------------------------------------------


class _PoolWakeSignal:
    """WakeSignal para pool de N workers via asyncio.Condition.

    wake_one() notifica a un solo waiter (el worker más antiguo en esperar).
    wake_all() notifica a todos (útil en shutdown o flood de trabajo).

    Compatible con la interfaz de MonoWorkerWakeSignal para que el
    ControlPlaneService.enqueue siga llamando wake_one() sin cambios.
    """

    def __init__(self, condition: asyncio.Condition) -> None:
        self._condition = condition

    def wake_one(self) -> None:
        """Despierta a UN worker ocioso (commit-then-wake, CTRL-P1-12)."""
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon(lambda: asyncio.ensure_future(self._notify_one()))
        except RuntimeError:
            pass

    def wake_all(self) -> None:
        """Despierta a TODOS los workers ociosos (shutdown / flood)."""
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon(lambda: asyncio.ensure_future(self._notify_all()))
        except RuntimeError:
            pass

    async def _notify_one(self) -> None:
        async with self._condition:
            self._condition.notify(1)

    async def _notify_all(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    async def wait_for_work(self, *, timeout: float) -> bool:  # noqa: ASYNC109
        """Espera hasta wake o timeout. Compatible con MonoWorkerWakeSignal."""
        async with self._condition:
            try:
                await asyncio.wait_for(
                    self._condition.wait(), timeout=timeout
                )
                return True
            except TimeoutError:
                return False
