"""CycleOutput: output de `ReasoningEngine.run_cycle`.

Contiene:
  - tool_call_proposals: lo que el agente propone hacer. NUNCA ejecutado.
  - narrative:           explicacion natural-language del razonamiento.
  - malformed_intents:   tool calls del LLM que no se parsearon bien (para audit).
  - rejected_by_policy:  propuestas rechazadas por el PolicyLayer pre-HITL
                         (con motivo). Sirven para audit y para detectar
                         intentos de prompt injection o errores del LLM.
  - usage:               tokens prompt/completion + coste estimado (multi-provider via LiteLLM).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hermes.domain.proposal import ToolCallProposal


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""


@dataclass(frozen=True, slots=True)
class RejectedProposal:
    proposal: ToolCallProposal
    reason: str
    policy_name: str


@dataclass(frozen=True, slots=True)
class CycleOutput:
    tool_call_proposals: tuple[ToolCallProposal, ...] = ()
    narrative: str = ""
    malformed_intents: tuple[dict[str, Any], ...] = ()
    rejected_by_policy: tuple[RejectedProposal, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    # CTRL-5 / TOP-1: True si el motor ejecutó al menos una tool READ que ingirió
    # contenido externo no confiable (web, Composio, fichero fuera del allowlist
    # de confianza). El orchestrator lo transfiere a ConsentContext para que el
    # broker fuerce HITL sobre TODAS las proposals del ciclo.
    read_external_content: bool = False
