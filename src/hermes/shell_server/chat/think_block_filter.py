"""ThinkBlockFilter — separa <think>...</think> de la respuesta final.

Stream-aware: maneja tags parciales (un chunk puede traer "<thi" y el
siguiente "nk>"). State machine simple:

  normal mode: emite texto como "delta" hasta encontrar <think>.
  thinking mode: emite texto como "thinking_delta" hasta encontrar </think>.

Útil para modelos reasoning (Qwen 3.x, DeepSeek-R1, GLM-4 Thinking, etc.)
que mezclan razonamiento intermedio con la respuesta final en el stream.

NO acoplado a LiteLLM — recibe deltas string, emite tuplas (kind, text).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_OPEN_TAG = "<think>"
_CLOSE_TAG = "</think>"

# Cuanto retener al final del buffer para no partir un tag a la mitad.
# El máximo es len(_CLOSE_TAG) - 1 = 7.
_MAX_BUFFER_RETENTION = 7


class ThinkBlockFilter:
    """State machine que separa el thinking del visible.

    Args:
        start_in_thinking: si True, asume modo thinking desde el primer
            chunk sin necesidad de ver `<think>`. Útil para modelos como
            Qwen 3.x con vLLM reasoning parser, que emiten el razonamiento
            "desnudo" + un `</think>` antes de la respuesta final.
    """

    def __init__(self, *, start_in_thinking: bool = False) -> None:
        self._mode = "thinking" if start_in_thinking else "normal"
        self._buffer = ""

    def process(self, delta: str) -> list[tuple[str, str]]:
        """Procesa un chunk delta. Devuelve lista de (kind, text)."""
        if not delta:
            return []
        self._buffer += delta
        results: list[tuple[str, str]] = []
        while True:
            if self._mode == "normal":
                idx = self._buffer.find(_OPEN_TAG)
                if idx == -1:
                    # No abre think. Retener últimos N chars (pueden ser tag parcial).
                    retain = min(_MAX_BUFFER_RETENTION, len(self._buffer))
                    safe_emit = self._buffer[: len(self._buffer) - retain]
                    if safe_emit:
                        results.append(("delta", safe_emit))
                        self._buffer = self._buffer[len(safe_emit) :]
                    break
                # Emit lo antes del <think>.
                if idx > 0:
                    results.append(("delta", self._buffer[:idx]))
                self._buffer = self._buffer[idx + len(_OPEN_TAG) :]
                self._mode = "thinking"
            else:  # thinking
                idx = self._buffer.find(_CLOSE_TAG)
                if idx == -1:
                    retain = min(_MAX_BUFFER_RETENTION, len(self._buffer))
                    safe_emit = self._buffer[: len(self._buffer) - retain]
                    if safe_emit:
                        results.append(("thinking_delta", safe_emit))
                        self._buffer = self._buffer[len(safe_emit) :]
                    break
                if idx > 0:
                    results.append(("thinking_delta", self._buffer[:idx]))
                self._buffer = self._buffer[idx + len(_CLOSE_TAG) :]
                self._mode = "normal"
        return results

    def flush(self) -> list[tuple[str, str]]:
        """Vacía el buffer pendiente al cierre del stream."""
        if not self._buffer:
            return []
        kind = "delta" if self._mode == "normal" else "thinking_delta"
        result = [(kind, self._buffer)]
        self._buffer = ""
        return result
