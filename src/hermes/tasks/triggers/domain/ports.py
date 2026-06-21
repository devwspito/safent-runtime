"""Puertos del subsistema `tasks/triggers` — fuentes de disparo autónomo (feature 007, US1/US3).

Fuentes de trabajo candidato. Las tres implementan sus propias corrutinas o
puntos de invocación y entregan trabajo EXCLUSIVAMENTE vía
TriggerEnqueueServicePort (el pre-gate de autorización-de-origen definido en
`tasks/triggers/domain/authorized_trigger_ports.py`). NINGUNA importa
WorkQueuePort directamente — el lint del grafo lo prohíbe para cerrar el
bypass del pre-gate.

Se enganchan al asyncio.gather del daemon como corrutinas paralelas hermanas de
orchestrator.run_forever(), bajo el mismo watchdog dedicado del pool.
gather(return_exceptions=True) aísla una fuente que muere; systemd
Restart=always + timers Persistent=yes son la red de seguridad (FR-023/SC-012).

Constitución I: NO toca BrowserPort/SurfaceAdapterPort/SelectorRegistry.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable


class AllowedSystemEvent(StrEnum):
    """Lista CERRADA de eventos del SO permitidos (FR-021).

    Cualquier evento fuera de este enum NO dispara nada (SC-010). El payload
    del evento es entrada NO confiable (FR-024): nunca eleva autoridad ni
    risk_ceiling.
    """

    SERVICE_FAILED = "service_failed"      # systemd1 Unit.PropertiesChanged ActiveState=failed
    BATTERY_LOW = "battery_low"            # UPower PropertiesChanged Percentage < umbral
    NETWORK_CHANGED = "network_changed"    # NetworkManager StateChanged
    DEVICE_CONNECTED = "device_connected"  # udev add


@runtime_checkable
class TimerTriggerSource(Protocol):
    """Fuente de temporizador (FR-020). Corrutina del gather del daemon.

    Duerme hasta el próximo disparo programado (entrada de la allow-list de
    orígenes de timer). Al vencer, construye un WorkItem candidato con
    available_at futuro y lo entrega vía TriggerEnqueueServicePort. Respaldada
    por systemd timers Persistent=yes como backstop de reinicio (recupera
    ejecuciones perdidas sin acumular ráfagas — spec Edge).
    """

    async def run_forever(self) -> None:
        """Bucle de la fuente. Termina con cancelación (shutdown del daemon).

        NUNCA encola directo: siempre vía enqueue_from_trigger (pre-gate).
        """
        ...


@runtime_checkable
class SystemEventTriggerSource(Protocol):
    """Fuente de evento del sistema (FR-021/FR-023). Corrutina del gather.

    Listener D-Bus push (signal_subscribe) — señalización, NO polling
    (NFR-005: cero consumo en reposo). Solo reacciona a AllowedSystemEvent.
    Marca el WorkItem candidato como derived_from_untrusted_content=True
    (taint → HITL forzado en el broker, FR-024). Bajo watchdog: su caída no
    detiene el agente y NO produce trabajo fantasma (solo construye candidatos;
    si muere, no hay encolado).
    """

    async def run_forever(self) -> None:
        """Suscribe las señales permitidas y, ante cada una, entrega un WorkItem
        candidato (con taint) vía enqueue_from_trigger. Fail-closed.
        """
        ...

    def allowed_events(self) -> frozenset[AllowedSystemEvent]:
        """Conjunto cerrado de eventos a los que esta fuente escucha."""
        ...


@runtime_checkable
class SelfEnqueueTriggerSource(Protocol):
    """Fuente de auto-encolado (FR-022). NO es corrutina de fondo.

    La invoca el orchestrator tras mark_completed, leyendo
    CycleOutput.follow_up.

    Valida ANTES de entregar el candidato a enqueue_from_trigger:
      - profundidad de cascada: parent.cascade_depth < 1 (una hija no engendra
        nieta — cap=1, SC-007). cascade_depth lo fija el servidor, no el LLM.
      - dedup_key OBLIGATORIO (None => rechazo).
      - presupuesto/hora por origen (consume_budget; agotado => rechazo + traza).
      - enqueued_by HEREDADO de la tarea madre (no del follow_up).
    """

    async def process_follow_up(
        self,
        *,
        parent_work_item_id: str,
        follow_up: FollowUpRequest,
    ) -> None:
        """Procesa un follow_up de una tarea recién completada.

        Encola COMO MUCHO una tarea hija vía enqueue_from_trigger, o la
        rechaza con traza si viola cap/dedup/presupuesto.
        Idempotente por dedup_key (índice UNIQUE parcial).
        """
        ...


class FollowUpRequest(Protocol):
    """Intención de seguimiento emitida por el agente en CycleOutput.follow_up
    (campo additivo, default None — no rompe P0/P1, Constitución I).

    Campos (data-model):
      instruction: str          — qué hacer en la tarea de seguimiento.
      dedup_key:   str          — OBLIGATORIO (FR-022). None => rechazo.
      priority:    int = 0

    NOTA: cascade_depth NO viaja aquí — lo deriva enqueue_from_trigger del
    parent.payload (server-side, anti-evasión del cap por el LLM).
    """

    @property
    def instruction(self) -> str: ...

    @property
    def dedup_key(self) -> str: ...

    @property
    def priority(self) -> int: ...
