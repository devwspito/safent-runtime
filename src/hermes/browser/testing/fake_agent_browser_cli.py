"""FakeAgentBrowserCli: doble de prueba determinista para AgentBrowserCliPort.

No requiere el binario agent-browser en CI. Los tests inyectan snapshots y
acciones pre-configuradas para verificar el comportamiento del
AgentBrowserDriver sin infraestructura real.

Uso tipico:
    cli = FakeAgentBrowserCli(
        snapshots=["@e1 [button] \"Enviar\"\\n@e2 [input] \"NIF\""],
        current_urls=["https://example.com"],
    )
    driver = AgentBrowserDriver(cli=cli)
    await driver.start()

Sigue el mismo patron que FakeMcpSession para mantener consistencia entre
los dos drivers opcionales.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeAgentBrowserCli:
    """CLI agent-browser falso que devuelve respuestas pre-configuradas.

    snapshots: lista de strings que snapshot() devuelve en orden.
               El ultimo elemento se repite si la lista se agota.
    current_urls: lista de URLs que current_url() devuelve en orden.
    """

    snapshots: list[str] = field(default_factory=lambda: [""])
    current_urls: list[str] = field(default_factory=lambda: ["about:blank"])

    # Call log para assertions en tests
    navigated_urls: list[str] = field(default_factory=list)
    clicked_refs: list[str] = field(default_factory=list)
    typed_calls: list[tuple[str, str]] = field(default_factory=list)  # (ref, text)
    snapshot_call_count: int = 0
    closed: bool = False

    # Contadores de indice
    _snapshot_idx: int = field(default=0, init=False, repr=False)
    _url_idx: int = field(default=0, init=False, repr=False)

    async def start(self) -> None:
        """No-op: el fake no requiere el binario."""

    async def navigate(self, url: str) -> None:
        self.navigated_urls.append(url)
        # Avanza el URL index para que current_url() refleje la navegacion
        if self._url_idx < len(self.current_urls) - 1:
            self._url_idx += 1

    async def snapshot(self) -> str:
        self.snapshot_call_count += 1
        idx = min(self._snapshot_idx, len(self.snapshots) - 1)
        result = self.snapshots[idx]
        if self._snapshot_idx < len(self.snapshots) - 1:
            self._snapshot_idx += 1
        return result

    async def click(self, ref: str) -> None:
        self.clicked_refs.append(ref)
        # Avanza snapshot index para simular cambio de pagina tras click
        if self._snapshot_idx < len(self.snapshots) - 1:
            self._snapshot_idx += 1

    async def type_(self, ref: str, text: str) -> None:
        self.typed_calls.append((ref, text))

    async def current_url(self) -> str:
        idx = min(self._url_idx, len(self.current_urls) - 1)
        return self.current_urls[idx]

    async def close(self) -> None:
        self.closed = True

    def set_next_snapshot(self, text: str) -> None:
        """Encola un snapshot que se devolvera en la proxima llamada a snapshot()."""
        self.snapshots.append(text)

    def inspect_calls(self) -> dict[str, Any]:
        """Resumen de todas las llamadas realizadas, para assertions compactas."""
        return {
            "navigated_urls": list(self.navigated_urls),
            "clicked_refs": list(self.clicked_refs),
            "typed_calls": list(self.typed_calls),
            "snapshot_call_count": self.snapshot_call_count,
            "closed": self.closed,
        }
