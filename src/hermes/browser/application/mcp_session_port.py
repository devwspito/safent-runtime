"""McpSessionPort: abstraccion del transporte MCP hacia @playwright/mcp.

Define la superficie minima que PlaywrightMcpDriver necesita del servidor MCP.
La implementacion real (StdioMcpSession) usa el SDK 'mcp' sobre stdio.
Los tests inyectan FakeMcpSession, que no requiere Node/npx/browser.

Formato del accessibility tree que devuelve browser_snapshot:
  Un string plano con lineas tipo:
      role=button name="Presentar definitivo" ref=e5
      role=link   name="Inicio"              ref=e12
  El driver parsea estas lineas para re-resolver refs en replay.
"""

from __future__ import annotations

from typing import Protocol


class McpSessionPort(Protocol):
    """Transporte MCP de bajo nivel para el servidor @playwright/mcp.

    Contrato minimo:
      - navigate(url)      : navega a la URL.
      - snapshot()         : devuelve el accessibility tree como string plano.
      - click(ref)         : hace click en el elemento con ese ref.
      - type_(ref, text)   : escribe texto en el elemento con ese ref.
      - press(key)         : presiona una tecla global (e.g. "Enter", "Tab").
    """

    async def navigate(self, url: str) -> None:
        """Navega a url."""
        ...

    async def snapshot(self) -> str:
        """Devuelve el accessibility tree como texto plano."""
        ...

    async def click(self, ref: str) -> None:
        """Click sobre el elemento identificado por ref."""
        ...

    async def type_(self, ref: str, text: str) -> None:
        """Escribe text en el elemento identificado por ref."""
        ...

    async def press(self, key: str) -> None:
        """Presiona una tecla global (e.g. Enter, Tab, Escape)."""
        ...

    async def current_url(self) -> str:
        """URL actual del browser gestionado por el servidor MCP."""
        ...

    async def screenshot(self) -> bytes:
        """Screenshot PNG del viewport actual."""
        ...

    async def close(self) -> None:
        """Cierra la sesion MCP y libera recursos."""
        ...
