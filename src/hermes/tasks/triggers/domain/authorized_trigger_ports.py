"""Gate de autorización-de-origen (feature 007, US2).

EL CORAZÓN default-deny. Vive en la domain layer de tasks/triggers (al lado de
ControlPlaneService.enqueue en tasks/control_plane), NUNCA en el broker (SRP /
FR-014): el broker valida QUÉ hace la tarea; ESTE módulo valida que el ORIGEN
esté autorizado.

Allow-list firmada, VACÍA por defecto. Ningún origen habilitado de fábrica
(SC-013). Habilitar uno es una acción posterior firmada/auditada por-origen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID


class AuthorizedTriggerType(StrEnum):
    """Tipos de origen auto-disparable. Whitelist positiva (FR-019).

    Cualquier valor fuera de este enum se rechaza (fail-closed).
    """

    TIMER = "timer"
    SYSTEM_EVENT = "system_event"
    SELF_ENQUEUE = "self_enqueue"


class RiskCeiling(StrEnum):
    """Techo de riesgo que un origen autorizado puede alcanzar.

    El evento NUNCA lo eleva (FR-024). Mapea a
    hermes.capabilities.domain.ports.RiskLevel.
    """

    LOW = "low"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class AuthorizedTrigger:
    """VO — registro FIRMADO que habilita a una fuente a encolar trabajo (US2).

    Invariantes:
      - la allow-list está vacía por defecto (entrega sin instancias enabled=1).
      - crearlo exige firma de admin auditable (created_by_admin_uuid del canal
        autenticado, NO del contenido — FR-013/NFR-003).
      - deshabilitarlo (enabled=False) corta el encolado de ese origen (FR-018).
      - scope acota a qué reacciona (p.ej. un timer específico, una unit, etc.).
      - allowed_capabilities + risk_ceiling acotan QUÉ puede disparar; el broker
        nunca recibe más de lo firmado.

    Campos P3 (scheduled-tasks): opcionales con default para no romper
    código que construya el VO sin los campos nuevos (tests, fixtures antiguas).
    """

    trigger_instance_id: UUID
    trigger_type: AuthorizedTriggerType
    scope_value: str                        # p.ej. cron expr, unit name, "*"
    allowed_capabilities: tuple[str, ...]   # Capability.value permitidas
    risk_ceiling: RiskCeiling
    created_by_admin_uuid: UUID             # AUTOR efectivo de las tareas (FR-016)
    authorized_at: datetime
    approval_signature: str                 # firma del admin (no-repudio)
    enabled: bool                           # revocable (kill por-origen)
    # ── P3: campos de calendario per-agent ──────────────────────────────────
    target_agent_id: str | None = None  # agente destino; None = usar el activo
    task_instruction: str = ""          # instrucción almacenada para el disparo
    one_shot: bool = False              # True = auto-revoca tras 1ª ejecución
    title: str = ""                     # etiqueta legible del calendario (UI)


@runtime_checkable
class AuthorizedTriggerRepositoryPort(Protocol):
    """Persistencia de la allow-list firmada.

    Tablas: authorized_trigger_types + authorized_trigger_instances.
    Default-deny: sin entrada habilitada, is_authorized devuelve None.
    """

    async def is_authorized(
        self, *, trigger_type: AuthorizedTriggerType, scope_value: str
    ) -> AuthorizedTrigger | None:
        """Devuelve el AuthorizedTrigger HABILITADO que cubre (tipo, scope).

        None si ninguno (fail-closed: None => no encolar). Resolución de scope
        según scope_validation del type.
        """
        ...

    async def authorize(
        self,
        *,
        trigger_type: AuthorizedTriggerType,
        scope_value: str,
        allowed_capabilities: tuple[str, ...],
        risk_ceiling: RiskCeiling,
        admin_uuid: UUID,
        approval_signature: str,
    ) -> AuthorizedTrigger:
        """Crea (o habilita) un origen.

        Acción privilegiada — admin_uuid deriva del canal autenticado
        (GetConnectionUnixUser), NUNCA del contenido.
        Emite audit TRIGGER_AUTHORIZED (el caller firma la cadena).
        """
        ...

    async def revoke(self, *, trigger_instance_id: UUID, admin_uuid: UUID) -> None:
        """Deshabilita un origen (enabled=False).

        Corta el encolado FUTURO de ese origen de inmediato (FR-018); NO altera
        tareas ya encoladas. Auditada.
        """
        ...

    async def consume_budget(self, *, trigger_instance_id: UUID) -> bool:
        """Token-bucket persistente por-origen (FR-022 presupuesto/hora).

        True si queda presupuesto (consume 1), False si agotado
        (=> rechazo + traza).
        """
        ...


@runtime_checkable
class TriggerEnqueueServicePort(Protocol):
    """EL PRE-GATE fail-closed (FR-015).

    Única puerta de las TriggerSources a la cola. Vive en
    tasks/triggers/application (junto a ControlPlaneService.enqueue). Reutiliza
    la ruta de ControlPlaneService.enqueue (rate-limit, _QUEUE_DEPTH_CAP, PII
    tokenize, commit-then-wake) y AÑADE el chequeo de autorización-de-origen.
    """

    async def enqueue_from_trigger(
        self,
        *,
        trigger_type: AuthorizedTriggerType,
        scope_value: str,
        instruction: str,
        dedup_key: str | None = None,
        priority: int = 0,
        derived_from_untrusted_content: bool = False,
        parent_work_item_id: UUID | None = None,    # solo self_enqueue
        target_agent_id: str | None = None,         # routing per-agent (calendario)
    ) -> UUID | None:
        """Flujo fail-closed (FR-015):

          1. repo.is_authorized(trigger_type, scope) -> None => audit
             TRIGGER_DENIED, NO encola, devuelve None (0 tareas — SC-001).
          2. Valida risk_ceiling/allowed_capabilities (la tarea no excede lo
             firmado) y, si self_enqueue, cap de cascada=1 + dedup obligatorio +
             consume_budget (FR-022). Falla cualquiera => no encola.
          3. enqueued_by = trigger.created_by_admin_uuid (timer/system_event) o
             el enqueued_by de la tarea madre (self_enqueue). NUNCA NULL/'system'
             (FR-016/CWE-862). Si no se puede derivar => fail-closed, no encola.
          4. Tokeniza PII, construye WorkItem(trigger_kind=trigger_type.value,
             trigger_instance_id=...), queue.enqueue (idempotente por dedup_key).
          5. Audit TRIGGER_ACTIVATED encadenado a TRIGGER_AUTHORIZED.
             commit-then-wake. La tarea entra IGUAL que una manual — el broker
             aplica consent/HITL/risk/audit SIN CAMBIOS (FR-017).

        Devuelve el task_id encolado, o None si se rechazó (fail-closed).
        """
        ...
