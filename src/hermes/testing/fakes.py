"""FakeReasoningEngine: implementacion deterministica de `ReasoningEngine` para tests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from hermes.domain.cycle_output import CycleOutput, TokenUsage
from hermes.domain.decision_context import DecisionContext
from hermes.domain.proposal import ToolCallProposal


@dataclass(frozen=True, slots=True)
class _ScriptedResponse:
    narrative: str = ""
    proposals: tuple[ToolCallProposal, ...] = field(default_factory=tuple)


def scripted_response(
    *,
    narrative: str = "",
    proposals: Sequence[ToolCallProposal] = (),
) -> _ScriptedResponse:
    """Construye una respuesta scripted para `FakeReasoningEngine`."""
    return _ScriptedResponse(narrative=narrative, proposals=tuple(proposals))


class FakeReasoningEngine:
    """Implementacion `ReasoningEngine` que devuelve respuestas scripted.

    Cada llamada a `run_cycle` consume la siguiente respuesta de la lista
    (round-robin si se acaba la lista). Util para tests del consumidor.
    """

    def __init__(self, scripted: Sequence[_ScriptedResponse] = ()) -> None:
        self._scripted: list[_ScriptedResponse] = list(scripted) or [_ScriptedResponse()]
        self._index = 0
        self.calls: list[DecisionContext] = []

    async def run_cycle(self, context: DecisionContext) -> CycleOutput:
        self.calls.append(context)
        response = self._scripted[self._index % len(self._scripted)]
        self._index += 1
        return CycleOutput(
            tool_call_proposals=response.proposals,
            narrative=response.narrative,
            malformed_intents=(),
            rejected_by_policy=(),
            usage=TokenUsage(model="fake"),
        )
