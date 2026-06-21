"""ConfidenceEvaluator: extrae confidence de una respuesta LLM.

Tres modos:
  1. Provider expone logprobs → promedio de log-probs de los tokens
     predichos → confidence en [0, 1].
  2. Sin logprobs + secondary_llm_call disponible → prompt corto de
     evaluación: "Rate confidence 0-1 of: '{intent}' → action: {action}".
     El evaluador parsea el float.
  3. Sin logprobs + sin secondary → confidence=1.0 (asume alta) +
     log warning. El HITL profundo recae en otros checks (DOM ambiguo,
     heurísticas, steps HIGH always-require-token).

Constitución V: sin dependencia de Chromium/Postgres/red.
Constitución III: el contexto enviado al evaluador secundario se tokeniza
  ANTES de llamar; responsabilidad del caller.

T706 — US5/Phase 7.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConfidenceConfig:
    """Configuración del evaluador de confianza.

    threshold: nivel mínimo para ejecutar sin HITL. Default 0.75.
    secondary_evaluator_model: si el provider no expone logprobs, usar
      este modelo para evaluación secundaria. None → asumir alta.
    """

    threshold: float = 0.75
    secondary_evaluator_model: str | None = None


class ConfidenceEvaluator:
    """Extrae confidence de una respuesta LLM o invoca evaluador secundario.

    Args:
        config: ConfidenceConfig.
        secondary_llm_call: callable async que recibe un prompt str y
          devuelve str. None → salta al modo 3 (asume alta).
    """

    def __init__(
        self,
        *,
        config: ConfidenceConfig,
        secondary_llm_call: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._config = config
        self._secondary_llm_call = secondary_llm_call

    async def evaluate(
        self,
        *,
        llm_response: dict | object,
        prompt: str,
    ) -> float:
        """Calcula confidence en [0, 1] para la respuesta dada.

        Args:
            llm_response: respuesta raw del provider (dict-like o objeto).
              Si expone logprobs, se usan directamente.
            prompt: intent/instrucción que generó la respuesta (para el
              evaluador secundario).

        Returns:
            float en [0, 1].
        """
        logprobs = _extract_logprobs(llm_response)
        if logprobs:
            return _logprobs_to_confidence(logprobs)

        if self._secondary_llm_call is not None:
            return await self._evaluate_secondary(llm_response, prompt)

        logger.warning(
            "hermes.browser.confidence.no_logprobs_no_secondary",
            extra={
                "note": (
                    "Provider no expone logprobs y no hay evaluador secundario. "
                    "Asumiendo confidence=1.0. HITL profundo basado en DOM/heurísticas."
                ),
            },
        )
        return 1.0

    async def _evaluate_secondary(
        self, llm_response: dict | object, prompt: str
    ) -> float:
        action = _extract_action_desc(llm_response)
        eval_prompt = (
            f"Rate confidence 0.0-1.0 that this action correctly fulfills "
            f"the intent.\nIntent: {prompt}\nAction: {action}\n"
            f"Reply with only a float between 0.0 and 1.0."
        )
        try:
            raw = await self._secondary_llm_call(eval_prompt)  # type: ignore[misc]
            return _parse_confidence_float(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser.confidence.secondary_failed",
                extra={"error": str(exc)},
            )
            return 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_logprobs(response: dict | object) -> list[float] | None:
    """Extrae lista de logprobs de una respuesta LiteLLM.

    LiteLLM con logprobs=True devuelve:
      response.choices[0].logprobs.content[*].logprob  (OpenAI style)
    o como dict:
      response["choices"][0]["logprobs"]["content"][*]["logprob"]

    Retorna None si no hay logprobs (provider no los expone).
    """
    try:
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if not choices:
                return None
            lp = choices[0].get("logprobs") or {}
            content = lp.get("content") or []
            vals = [t["logprob"] for t in content if isinstance(t, dict) and "logprob" in t]
        else:
            choices = getattr(response, "choices", None) or []
            if not choices:
                return None
            lp = getattr(choices[0], "logprobs", None)
            if lp is None:
                return None
            content = getattr(lp, "content", None) or []
            vals = [
                getattr(t, "logprob", None)
                for t in content
                if getattr(t, "logprob", None) is not None
            ]

        return vals if vals else None
    except Exception:  # noqa: BLE001
        return None


def _logprobs_to_confidence(logprobs: list[float]) -> float:
    """Convierte lista de log-probs a confidence en [0, 1].

    Usa el mínimo para ser conservador: el token menos probable del
    tool-call es el que más incertidumbre indica.
    min_logprob → exp(min_logprob) → confidence.
    Clampado a [0, 1].
    """
    if not logprobs:
        return 1.0
    min_lp = min(logprobs)
    return max(0.0, min(1.0, math.exp(min_lp)))


def _extract_action_desc(response: dict | object) -> str:
    """Extrae descripción textual de la acción para el evaluador secundario."""
    try:
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if choices:
                msg = choices[0].get("message", {})
                return str(msg.get("content") or msg.get("tool_calls", ""))
        else:
            choices = getattr(response, "choices", None) or []
            if choices:
                msg = getattr(choices[0], "message", None)
                if msg:
                    return str(getattr(msg, "content", "") or getattr(msg, "tool_calls", ""))
    except Exception:  # noqa: BLE001,S110
        pass
    return str(response)[:200]


def _parse_confidence_float(raw: str) -> float:
    """Parsea un float de la respuesta del evaluador secundario."""
    stripped = raw.strip()
    for token in stripped.split():
        try:
            val = float(token.strip(".,;:"))
            return max(0.0, min(1.0, val))
        except ValueError:
            continue
    return 1.0
