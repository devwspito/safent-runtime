"""ToolSpec: descripcion declarativa de una tool que la vertical expone al LLM.

Cada vertical declara sus tools como `ToolSpec`. Hermes:
  1. Convierte ToolSpec → formato OpenAI function-calling (litellm-compatible).
  2. Si `risk == WRITE_*` -> bloquea ejecucion en `CapturingToolHost` y captura
     la invocacion como `ToolCallProposal`.
  3. Si `risk == READ_ONLY` -> permite ejecutar el handler (la vertical
     entrega un callable lado-Python; Hermes lo invoca y devuelve resultado al LLM).

Diseno: la vertical NO importa LiteLLM; solo declara ToolSpec. Hermes hace el bridge.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ToolRisk(StrEnum):
    """Clasificacion de riesgo del tool.

    READ_ONLY:            consulta. Hermes la ejecuta y devuelve resultado al LLM.
    WRITE_PROPOSAL:       escritura "interna" reversible (proponer borrador,
                          guardar nota). Capturada como ToolCallProposal — la
                          vertical decide si requiere HITL o auto-execute.
    WRITE_EXECUTE:        escritura que el LLM puede proponer auto-ejecutable
                          si pasa PolicyLayer (importes pequenos, low-risk).
                          El consumer puede tratarla como WRITE_PROPOSAL si quiere.
    EXTERNA_IRREVERSIBLE: escritura externa irreversible (presentar modelo,
                          firma electronica, pago, baja efectiva). SIEMPRE HITL
                          con TOTP del titular. Hermes nunca la ejecuta.
    """

    READ_ONLY = "read_only"
    WRITE_PROPOSAL = "write_proposal"
    WRITE_EXECUTE = "write_execute"
    EXTERNA_IRREVERSIBLE = "externa_irreversible"


# Handler signature: async function taking validated args (JSON schema) and
# returning a JSON-serialisable result that Hermes returns to the LLM.
ReadHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Declaracion de una tool que la vertical expone al LLM.

    Atributos:
        name:        nombre de la tool (snake_case). Unico por vertical.
        description: descripcion para el LLM (importante: bien escrita = mejor uso).
        parameters_schema: JSON schema de parametros (formato OpenAI function-calling).
        risk:        clasificacion de riesgo.
        entity_type: tipo semantico que la tool toca (campaign, cliente, mascota...).
                     Usado por `CapturingToolHost` para enriquecer la `ToolCallProposal`.
        handler:     callable async que ejecuta la tool. SOLO usado si risk == READ_ONLY.
                     Para writes, debe ser None (la vertical es quien ejecuta tras HITL).
    """

    name: str
    description: str
    parameters_schema: dict[str, Any]
    risk: ToolRisk
    entity_type: str = ""
    handler: ReadHandler | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("name is required")
        if not self.description:
            raise ValueError("description is required")
        if self.risk == ToolRisk.READ_ONLY and self.handler is None:
            raise ValueError(
                f"tool {self.name!r}: READ_ONLY tools must provide a handler"
            )
        if self.risk != ToolRisk.READ_ONLY and self.handler is not None:
            raise ValueError(
                f"tool {self.name!r}: only READ_ONLY tools may provide a handler; "
                "writes go through HITL on the vertical side"
            )

    def to_openai_function(self) -> dict[str, Any]:
        """Serializar al formato function-calling de OpenAI/LiteLLM."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

    @property
    def is_write(self) -> bool:
        return self.risk != ToolRisk.READ_ONLY
