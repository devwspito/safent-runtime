"""InMemoryControlPlaneChannel — fake de ControlPlaneChannelPort para tests.

Constitución V: tests base corren sin WS real, sin TLS, sin red.
Registra todos los comandos enviados para que los tests los puedan inspeccionar.
"""

from __future__ import annotations

from typing import Any

__all__ = ["InMemoryControlPlaneChannel"]


class InMemoryControlPlaneChannel:
    """Fake de ControlPlaneChannelPort.

    Uso en tests::

        channel = InMemoryControlPlaneChannel()
        # ...tras código bajo prueba...
        assert channel.has_command("audit_entry", audit_kind="whisper_model_tampered")
    """

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def send_command(self, method: str, params: dict[str, Any]) -> None:
        self.commands.append((method, dict(params)))

    def has_command(self, method: str, **param_filters: Any) -> bool:
        """Comprueba si se emitió un comando con los parámetros dados."""
        for m, p in self.commands:
            if m == method and all(p.get(k) == v for k, v in param_filters.items()):
                return True
        return False

    def commands_of(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.commands if m == method]

    def clear(self) -> None:
        self.commands.clear()
