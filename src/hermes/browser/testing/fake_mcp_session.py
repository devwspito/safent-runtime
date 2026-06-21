"""FakeMcpSession: doble de prueba determinista para McpSessionPort.

No requiere Node/npx/browser en CI. Los tests inyectan snapshots y acciones
pre-configuradas para verificar el comportamiento del PlaywrightMcpDriver
sin infraestructura real.

Uso tipico:
    session = FakeMcpSession(
        snapshots=["role=button name='Enviar' ref=e1"],
        current_urls=["https://example.com"],
    )
    driver = PlaywrightMcpDriver(session=session)
    await driver.start()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeMcpSession:
    """Sesion MCP falsa que devuelve respuestas pre-configuradas.

    snapshots: lista de strings que snapshot() devuelve en orden.
               El ultimo elemento se repite si la lista se agota.
    current_urls: lista de URLs que current_url() devuelve en orden.
    screenshot_bytes: bytes que screenshot() siempre devuelve.
    """

    snapshots: list[str] = field(default_factory=lambda: [""])
    current_urls: list[str] = field(default_factory=lambda: ["about:blank"])
    screenshot_bytes: bytes = b"\x89PNG\r\n\x1a\n"

    # Call log for assertions
    navigated_urls: list[str] = field(default_factory=list)
    clicked_refs: list[str] = field(default_factory=list)
    typed_calls: list[tuple[str, str]] = field(default_factory=list)  # (ref, text)
    pressed_keys: list[str] = field(default_factory=list)
    snapshot_call_count: int = 0
    closed: bool = False

    # Index counters
    _snapshot_idx: int = field(default=0, init=False, repr=False)
    _url_idx: int = field(default=0, init=False, repr=False)

    async def navigate(self, url: str) -> None:
        self.navigated_urls.append(url)
        # Advance URL index on navigate so next current_url() returns the new value
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
        # Advance snapshot index to simulate page change after click
        if self._snapshot_idx < len(self.snapshots) - 1:
            self._snapshot_idx += 1

    async def type_(self, ref: str, text: str) -> None:
        self.typed_calls.append((ref, text))

    async def press(self, key: str) -> None:
        self.pressed_keys.append(key)

    async def current_url(self) -> str:
        idx = min(self._url_idx, len(self.current_urls) - 1)
        return self.current_urls[idx]

    async def screenshot(self) -> bytes:
        return self.screenshot_bytes

    async def close(self) -> None:
        self.closed = True

    def set_next_snapshot(self, text: str) -> None:
        """Reemplaza el snapshot que se devolvera en la proxima llamada a snapshot()."""
        self.snapshots.append(text)

    def inspect_calls(self) -> dict[str, Any]:
        """Resumen de todas las llamadas realizadas, para assertions compactas."""
        return {
            "navigated_urls": list(self.navigated_urls),
            "clicked_refs": list(self.clicked_refs),
            "typed_calls": list(self.typed_calls),
            "pressed_keys": list(self.pressed_keys),
            "snapshot_call_count": self.snapshot_call_count,
            "closed": self.closed,
        }
