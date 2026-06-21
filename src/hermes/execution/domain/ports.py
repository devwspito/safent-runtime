"""Puertos del bounded context `execution` — feature 006 / PIEZA 4.

Generalizacion del InputOwnershipLedger de teaching
(`agents_os/application/teaching/input_ownership_ledger.py`) a un registro de
contextos de ejecucion fail-closed con UN dueno por superficie de input.

DDD/SRP: el registro de propiedad (logico) vive aqui; el aislamiento FISICO
(browser --session / display headless) vive en FACTORIES (no en puertos del
browser) -> Constitucion I (FR-028): NO toca BrowserPort/SelectorRegistry/
BrowserSession/StorageStatePort.
Constitucion IV / NFR-004: fail-closed -> ante duda sobre propiedad, denegar.

Reuse: el patron de un-dueno-por-context_id + RLock + idempotente-por-mismo-owner
de InputOwnershipLedger se conserva; aqui se generaliza la CLAVE (de context_id
UUID a InputSurfaceKey) y el OWNER (de InputOwner fijo a ExecutionContextId).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID


class InputOwnershipViolation(RuntimeError):
    """Tomar una superficie ya poseida por OTRO dueno (FR-022, fail-closed).
    La negacion deja traza (auditada por el caller)."""


class InputSurfaceKind(StrEnum):
    """Superficie de input/display EXCLUSIVA (un dueno a la vez, FR-021).

    Distinta de agents_os.domain.surface_kind.SurfaceKind (taxonomia de
    CAPTURA de acciones). Aqui modelamos el RECURSO fisico/logico exclusivo.
    """

    KEYBOARD = "keyboard"
    MOUSE = "mouse"
    SCREEN = "screen"
    BROWSER = "browser"


class InputOwnerKind(StrEnum):
    """Quien puede poseer una superficie. OPERATOR = humano (chat/teaching);
    AGENT_TASK = un contexto de ejecucion del agente."""

    OPERATOR = "operator"
    AGENT_TASK = "agent_task"


@dataclass(frozen=True, slots=True)
class InputSurfaceKey:
    """Identidad de una superficie exclusiva. Dos claves iguales colisionan
    (un solo dueno). `surface_id` distingue p.ej. dos browsers --session
    distintos (cada uno su propia superficie aislada -> NO colisionan)."""

    kind: InputSurfaceKind
    surface_id: str  # p.ej. session-name del browser, display id headless


@dataclass(frozen=True, slots=True)
class ExecutionContextId:
    """Identidad del dueno de una superficie: una tarea/worker o el operador."""

    value: UUID
    owner_kind: InputOwnerKind


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Ambito aislado en el que una tarea opera sobre superficies (Key Entity).

    Invariantes:
      - a lo sumo UN dueno por InputSurfaceKey en todo momento (FR-021).
      - tomar una superficie ocupada por otro dueno -> InputOwnershipViolation.
      - al liberar/fallar, la superficie vuelve a estar disponible (FR-023).
      - ningun dueno huerfano sobrevive a un reinicio (registry in-memory +
        reconcile al boot, FR-026/SC-010).
    """

    context_id: ExecutionContextId
    surface: InputSurfaceKey
    isolation_key: str  # session-name / display aislado (factory-provided)


@runtime_checkable
class ExecutionContextRegistryPort(Protocol):
    """Registro fail-closed de UN dueno por superficie de input (FR-021..FR-023,
    FR-026, NFR-004). Generaliza InputOwnershipLedger. Thread/worker-safe.

    Implementacion: in-memory + RLock (como el ledger de teaching). Una por
    proceso-daemon. El pool de workers lo consulta ANTES de tomar una superficie.
    """

    def claim(self, *, surface: InputSurfaceKey, owner: ExecutionContextId) -> None:
        """Reclama `surface` para `owner`. Idempotente con el MISMO owner.

        Raises:
            InputOwnershipViolation: si otro owner ya la posee (fail-closed,
                la negacion deja traza para el caller).
        """
        ...

    def owner_of(self, *, surface: InputSurfaceKey) -> ExecutionContextId | None:
        """Dueno actual, o None si libre."""
        ...

    def release(self, *, surface: InputSurfaceKey) -> None:
        """Libera la superficie (re-reclamable). No-op si ya libre (cleanup-safe,
        FR-023). Invocado en el `finally` del worker."""
        ...

    def release_all_for(self, *, owner: ExecutionContextId) -> int:
        """Libera TODAS las superficies de un dueno (worker que termina/falla,
        sin fugas). Devuelve el numero liberado."""
        ...

    def reconcile(self) -> int:
        """Limpia TODO dueno al arranque del daemon (FR-026): tras un reinicio
        ningun dueno huerfano debe sobrevivir bloqueando una superficie. Devuelve
        el numero de entradas purgadas. Las tareas IN_PROGRESS se reconcilian
        aparte via la cola (reconcile_stale).
        """
        ...


@runtime_checkable
class ExecutionContextFactory(Protocol):
    """Crea/destruye contextos de ejecucion AISLADOS (FR-028, Constitucion I).

    El aislamiento FISICO (browser --session distinto, display headless por
    contexto) ocurre AQUI, en la factory -> NUNCA modificando BrowserPort/
    StorageStatePort. Analogo a TeachingContextFactory (spec 004) que ya
    arranca browsers con session-name aislado. Reuse del patron probado.

    La factory registra el dueno en ExecutionContextRegistryPort al abrir y lo
    libera al cerrar (FR-021/FR-023).
    """

    async def open(
        self,
        *,
        context_id: ExecutionContextId,
        surface_kind: InputSurfaceKind,
        isolation_seed: str,
    ) -> ExecutionContext:
        """Abre un contexto aislado y reclama su superficie en el registry.
        `isolation_seed` deriva el session-name/display unico. Fail-closed: si
        la superficie ya esta tomada -> InputOwnershipViolation (no comparte).
        """
        ...

    async def close(self, *, context_id: ExecutionContextId) -> None:
        """Cierra el contexto y libera TODAS sus superficies (sin fugas)."""
        ...
