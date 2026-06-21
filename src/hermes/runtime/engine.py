"""ReasoningEngine: Protocol que toda implementacion del motor agentico cumple.

Existen, ahora, dos implementaciones:
  - `LiteLLMReasoningEngine` (real, recomendada).
  - `FakeReasoningEngine` en tests/_fakes/ (no-op deterministico para tests del consumidor).

La vertical SOLO depende del Protocol; nunca de la implementacion concreta.
"""

from __future__ import annotations

from typing import Protocol

from hermes.domain.cycle_output import CycleOutput
from hermes.domain.decision_context import DecisionContext


class ReasoningEngine(Protocol):
    """Contrato del motor de razonamiento.

    Inputs: DecisionContext (que disparo el ciclo + datos del dominio).
    Output: CycleOutput (propuestas + narrative + audit metadata).

    Invariantes garantizadas por TODA implementacion:
      1. NO ejecuta writes externamente. Solo propone via tool_calls.
      2. Tokeniza PII antes de salir al provider externo.
      3. Valida toda propuesta con PolicyLayer antes de devolverla.
      4. Audit trace incluye tokens consumidos + coste + modelo usado.
    """

    async def run_cycle(self, context: DecisionContext) -> CycleOutput: ...
