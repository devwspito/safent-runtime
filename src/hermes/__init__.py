"""Hermes runtime: motor agentico LLM reutilizable.

Cada vertical inyecta:
    - PersonaSpec (system prompt, reglas, tono).
    - ToolSpec list (que tools tiene disponibles).
    - PIITokenizer (que datos sensibles enmascarar antes del LLM).
    - PolicyLayer (validacion pre-HITL de las propuestas).
    - PromptBuilder opcional (override del builder por defecto).

Hermes garantiza:
    - LLM PROPONE; nunca ejecuta writes (CapturingToolHost).
    - PII salida tokenizada; nunca raw fuera del proceso.
    - Propuestas validadas por PolicyLayer antes de pasarlas a la cola HITL del consumidor.
    - Multi-provider via LiteLLM (no atado a Anthropic/OpenAI/Azure).

Punto de entrada minimo:
    from hermes import (
        ReasoningEngine,
        LiteLLMReasoningEngine,
        DecisionContext,
        CycleOutput,
        ToolCallProposal,
        PersonaSpec,
        ToolSpec,
        ToolRisk,
        PIITokenizer,
        DefaultPIITokenizer,
        PolicyLayer,
        DefaultPolicyLayer,
        PromptBuilder,
        DefaultPromptBuilder,
    )
"""

from hermes.domain.cycle_output import CycleOutput
from hermes.domain.decision_context import DecisionContext
from hermes.domain.proposal import ToolCallProposal
from hermes.domain.tool_spec import ToolRisk, ToolSpec
from hermes.policies.layer import DefaultPolicyLayer, PolicyLayer, PolicyVerdict
from hermes.prompts.builder import DefaultPromptBuilder, PromptBuilder
from hermes.prompts.persona import PersonaSpec
from hermes.runtime.engine import ReasoningEngine
from hermes.runtime.tool_host import CapturingToolHost
from hermes.tokenizer.pii import DefaultPIITokenizer, PIITokenizer, TokenizedPayload



__all__ = [
    "CapturingToolHost",
    "CycleOutput",
    "DecisionContext",
    "DefaultPIITokenizer",
    "DefaultPolicyLayer",
    "DefaultPromptBuilder",
    "LiteLLMReasoningEngine",
    "PersonaSpec",
    "PIITokenizer",
    "PolicyLayer",
    "PolicyVerdict",
    "PromptBuilder",
    "ReasoningEngine",
    "TokenizedPayload",
    "ToolCallProposal",
    "ToolRisk",
    "ToolSpec",
]

__version__ = "0.8.4"  # single source of truth: repo-root VERSION (synced by build.sh)
