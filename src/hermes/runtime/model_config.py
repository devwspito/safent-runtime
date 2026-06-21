"""ModelConfig + errores — origen neutro, sin acoplamiento a ningún engine.

Antes vivía en `litellm_engine.py` (motor que ya no existe). Lo dejamos en su
propio módulo para que callers (nous_engine, provider_config_source, callers
históricos) no tengan que importar de un módulo de engine.
"""

from __future__ import annotations

import json
import logging
import os

from hermes.domain.decision_context import DecisionContext
from hermes.tokenizer.pii import TokenizedPayload
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("hermes.runtime.model_config")

_DEFAULT_MAX_ITERATIONS = 8
_DEFAULT_TIMEOUT_S = 90
_DEFAULT_TEMPERATURE = 0.0


class HermesModelNotConfiguredError(RuntimeError):
    """HERMES_MODEL no está definido (ni en env, ni en ModelConfig, ni en provider)."""


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """Override explícito de la configuración del modelo."""

    model: str
    api_key: str | None = None
    base_url: str | None = None
    max_iterations: int = _DEFAULT_MAX_ITERATIONS
    timeout_seconds: int = _DEFAULT_TIMEOUT_S
    temperature: float = _DEFAULT_TEMPERATURE
    # Tope de generación por turno. None = sin tope. Imprescindible con modelos
    # de razonamiento: sin tope la traza thinking se desboca hasta max_model_len.
    max_tokens: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> ModelConfig:
        model = os.environ.get("HERMES_MODEL", "")
        if not model:
            raise HermesModelNotConfiguredError(
                "HERMES_MODEL no está definido. Ejemplos: "
                "'openai-api/gpt-4o-mini', 'anthropic/claude-3-5-sonnet-20241022'."
            )
        return cls(
            model=model,
            api_key=os.environ.get("HERMES_API_KEY") or None,
            base_url=os.environ.get("HERMES_MODEL_BASE_URL") or None,
            **_operational_knobs_from_env(),
        )

    @classmethod
    def from_provider(
        cls, *, model: str, api_key: str | None, base_url: str | None
    ) -> ModelConfig:
        """ModelConfig desde el provider configurado por el usuario."""
        return cls(
            model=model,
            api_key=api_key,
            base_url=base_url,
            **_operational_knobs_from_env(),
        )

    @classmethod
    def from_env_with_prefix(cls, prefix: str) -> ModelConfig:
        """Soporta AUDITOR_MODEL / AUDITOR_API_KEY, cayendo a HERMES_* si no existe."""
        model = os.environ.get(f"{prefix}MODEL") or os.environ.get("HERMES_MODEL", "")
        if not model:
            raise HermesModelNotConfiguredError(
                f"{prefix}MODEL ni HERMES_MODEL definidos."
            )
        return cls(
            model=model,
            api_key=(
                os.environ.get(f"{prefix}API_KEY")
                or os.environ.get("HERMES_API_KEY")
                or None
            ),
            base_url=(
                os.environ.get(f"{prefix}BASE_URL")
                or os.environ.get("HERMES_MODEL_BASE_URL")
                or None
            ),
            **_operational_knobs_from_env(),
        )


def _operational_knobs_from_env() -> dict[str, Any]:
    return {
        "max_iterations": int(
            os.environ.get("HERMES_MAX_ITERATIONS", str(_DEFAULT_MAX_ITERATIONS))
        ),
        "timeout_seconds": int(
            os.environ.get("HERMES_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_S))
        ),
        "temperature": float(
            os.environ.get("HERMES_TEMPERATURE", str(_DEFAULT_TEMPERATURE))
        ),
        "max_tokens": (
            int(os.environ["HERMES_MAX_TOKENS"])
            if os.environ.get("HERMES_MAX_TOKENS")
            else None
        ),
        "extra": _extra_from_env(),
    }


def _extra_from_env() -> dict[str, Any]:
    raw = os.environ.get("HERMES_EXTRA_BODY")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"HERMES_EXTRA_BODY no es JSON válido: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("HERMES_EXTRA_BODY debe ser un objeto JSON (dict).")
    return {"extra_body": parsed}


def _replace_context(
    context: DecisionContext, tokenized: TokenizedPayload
) -> DecisionContext:
    """Construye un DecisionContext con los payloads tokenizados."""
    safe = tokenized.sanitized
    if not isinstance(safe, dict):
        return context
    return DecisionContext(
        tenant_id=context.tenant_id,
        cycle_id=context.cycle_id,
        trigger=context.trigger,
        subjects=tuple(safe.get("subjects", list(context.subjects))),
        constraints=safe.get("constraints", context.constraints),
        operator_instruction=context.operator_instruction,
        domain_payload=safe.get("domain_payload", context.domain_payload),
        metadata=context.metadata,
        created_at=context.created_at,
    )
