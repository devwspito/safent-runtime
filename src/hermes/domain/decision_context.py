"""DecisionContext: input al ciclo del agente.

Genérico para cualquier vertical. Los campos `domain_payload` y `metadata` son
opacos a Hermes — los rellena cada vertical con sus entidades de dominio.

Hermes solo lee:
  - tenant_id    (para audit + multi-tenancy guards).
  - cycle_id     (correlation id, propaga al audit log).
  - trigger      (qué disparó este ciclo: regla, evento, cron, human request).
  - subjects     (a quién/qué afecta: NIF, lead_id, fund_id, campaign_id, mascota_id…).
  - constraints  (límites operativos: importe máx, deadline, etc.).
  - domain_payload (dict opaco con los datos del dominio).
  - metadata     (libre uso del caller).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """Input a `ReasoningEngine.run_cycle`.

    Inmutable. Los placeholders PII se aplican sobre `domain_payload` y
    `subjects` por el `PIITokenizer` antes de pasar al LLM.
    """

    tenant_id: UUID
    cycle_id: UUID
    trigger: str
    subjects: tuple[str, ...] = ()
    constraints: dict[str, Any] = field(default_factory=dict)
    # Instrucción CONFIABLE del operador autenticado (el mensaje de chat / la
    # tarea). Se renderiza como la tarea a ejecutar, FUERA del sobre untrusted.
    # Los DATOS (domain_payload, subjects) siguen untrusted (defensa anti-
    # injection). La ejecución de acciones la gatea igual el broker (consent/HITL).
    operator_instruction: str = ""
    # Agente (del roster multi-agente) al que pertenece esta tarea. El daemon
    # resuelve la persona efectiva por ciclo desde aquí. None = agente activo.
    agent_id: str | None = None
    domain_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def __post_init__(self) -> None:
        if not self.trigger:
            raise ValueError("trigger is required")
