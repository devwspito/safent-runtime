"""ToolCallProposal: propuesta de accion capturada por CapturingToolHost.

Hermes nunca ejecuta writes — la propuesta se entrega al consumidor (cada
vertical) que la encola en su HITL queue, la presenta al operador, y solo
ejecuta tras aprobacion explicita (TOTP cuando aplica).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ToolCallProposal:
    """Propuesta inmutable de invocacion de tool.

    Atributos:
        proposal_id: identifier unico de esta propuesta (idempotencia HITL).
        tool_name:   nombre exacto de la tool tal como la define la vertical.
        tenant_id:   tenant emisor (multi-tenant isolation).
        entity_id:   identifier del objeto sobre el que actua (ID interno
                     de la vertical: campaign_id, NIF cliente, mascota_id, …).
        entity_type: tipo semantico del entity_id ("campaign", "cliente",
                     "mascota", "fund", …). Define la vertical.
        parameters:  parametros tokenizados de la llamada (placeholders PII
                     ya aplicados; el consumer rehidrata antes de mostrar al humano).
        justification: explicacion natural-language del LLM de por que propone esta accion.
    """

    proposal_id: UUID
    tool_name: str
    tenant_id: UUID
    entity_id: str
    entity_type: str
    parameters: dict[str, Any]
    justification: str

    def __post_init__(self) -> None:
        if not self.tool_name:
            raise ValueError("tool_name is required")
        if not self.entity_id:
            raise ValueError("entity_id is required")
        if not self.entity_type:
            raise ValueError("entity_type is required")
