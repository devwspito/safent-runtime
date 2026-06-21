"""Puerto del POOL de workers — feature 006 / PIEZA 4.

Refactor de AgentLoopOrchestrator.run_forever (hoy while-True mono-worker,
`tasks/application/agent_loop_orchestrator.py:107-122`) a N workers concurrentes.

DISeNO CLAVE (Assumption firmada): el wake-on-enqueue (PIEZA 5, WorkerWakeSignal)
se abstrae ANTES del pool para que pasar de mono a N NO obligue a re-refactorizar
la senalizacion. Cada worker ejecuta el MISMO ciclo claim->process->mark actual
(sin cambiar _process ni la cola). La cola SQLite ya garantiza claim atomico sin
doble-toma (BEGIN IMMEDIATE) -> N workers compiten de forma segura.

NFR-006: el watchdog (sd_notify WATCHDOG=1) se emite desde un LATIDO DEDICADO,
independiente de los workers -> una tarea larga en un worker NO detiene el latido.
FR-024: un worker ocupado no bloquea a los demas. FR-025: exceso espera por
prioridad en la cola (sin perdida ni doble-toma).

Constitucion I: NO toca BrowserPort/SelectorRegistry. El aislamiento de input
entre workers lo da ExecutionContextRegistryPort (fail-closed) + las factories.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class WorkerPoolPort(Protocol):
    """Conjunto de N workers que drenan la cola concurrentemente (FR-020).

    Reemplaza el run_forever mono. Cada worker:
      1. emite/participa del latido (el latido dedicado, no per-worker).
      2. respeta is_paused() (kill-switch global).
      3. wait_for_work(timeout) sobre WorkerWakeSignal (wake-on-enqueue).
      4. claim_next() atomico -> _process(item) (logica intacta).
      5. toma superficies via ExecutionContextRegistryPort (fail-closed).

    Implementacion: N corrutinas asyncio en el daemon (trabajo I/O-bound:
    LLM + adapters). NO multiprocessing (cruzaria el asyncio.Event del wake).
    """

    async def start(self, *, size: int) -> None:
        """Arranca `size` workers (default HERMES_WORKER_POOL_SIZE, p.ej. 4 =
        umbral SC-007). Llamado tras bootstrap() y tras notificar READY=1
        (liveness no depende del numero de workers).
        """
        ...

    async def drain_and_stop(self) -> None:
        """Parada limpia (SIGTERM): deja de tomar trabajo nuevo, espera a que los
        workers en curso terminen su item actual (no hot-abort -> Out-of-Scope),
        libera contextos de ejecucion. La cola persiste lo no drenado.
        """
        ...

    def active_worker_count(self) -> int:
        """Numero de workers procesando un item ahora mismo (observabilidad +
        GetQueueStatus.in_progress). 0 = pool ocioso-sano.
        """
        ...
