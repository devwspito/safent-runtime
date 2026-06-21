"""WorkspaceLifecyclePort fake en memoria — testing-only.

Constitución V: cualquier puerto público tiene un fake obligatorio aquí
para que los tests base puedan correr sin VM real, sin Chromium, sin LLM.

Los métodos quedan como ``NotImplementedError`` hasta que las tareas de US1+
en adelante (T065+) los rellenen con comportamiento útil para los tests.
"""

from __future__ import annotations

from typing import Any


class InMemoryWorkspaceLifecycle:
    """Esqueleto — implementar cuando lo requiera la story correspondiente."""

    def __init__(self) -> None:
        self._state: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:  # pragma: no cover
        raise NotImplementedError(
            f"InMemoryWorkspaceLifecycle.{name}() todavía no implementado — pendiente de tareas US1+."
        )
