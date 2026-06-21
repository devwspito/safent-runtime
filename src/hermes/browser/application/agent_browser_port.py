"""AgentBrowserCliPort: abstraccion del CLI agent-browser (vercel-labs).

Define la superficie minima que AgentBrowserDriver necesita del CLI.
La implementacion real (AgentBrowserCli) invoca el binario Rust via subprocess.
Los tests inyectan FakeAgentBrowserCli, que no requiere el binario instalado.

Formato del accessibility tree que devuelve snapshot():
  Un string con lineas tipo (accessibility tree de agent-browser):
      @e1 [heading] "Log in"
      @e2 [form]
        @e3 [input type="email"] placeholder="Email"
        @e5 [button type="submit"] "Continue"
  El driver parsea estas lineas para re-resolver refs en replay.

El snapshot incluye una cabecera Page/URL:
    Page: Example - Log in
    URL: https://example.com/login

Los refs @eN son EFIMEROS: expiran con cada snapshot. El driver almacena
la identidad semantica durable (role + accessible_name) para replay.
"""

from __future__ import annotations

from typing import Protocol


class AgentBrowserCliPort(Protocol):
    """Transporte CLI de bajo nivel para el binario agent-browser.

    Contrato minimo:
      - navigate(url)       : navega a la URL (open <url>).
      - snapshot()          : devuelve el accessibility tree como string plano.
      - click(ref)          : hace click en @eN (click @eN).
      - type_(ref, text)    : escribe texto en @eN (fill @eN "text").
      - current_url()       : URL actual del browser.
      - close()             : cierra la sesion del daemon.
    """

    async def navigate(self, url: str) -> None:
        """Navega a url."""
        ...

    async def snapshot(self) -> str:
        """Devuelve el accessibility tree como texto plano (snapshot -i)."""
        ...

    async def click(self, ref: str) -> None:
        """Click sobre el elemento identificado por @eN."""
        ...

    async def type_(self, ref: str, text: str) -> None:
        """Escribe text en el elemento identificado por @eN (fill @eN "text")."""
        ...

    async def current_url(self) -> str:
        """URL actual del browser gestionado por el daemon agent-browser."""
        ...

    async def close(self) -> None:
        """Cierra la sesion agent-browser y libera recursos."""
        ...
