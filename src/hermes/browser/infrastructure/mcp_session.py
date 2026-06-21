"""StdioMcpSession: adaptador real del servidor @playwright/mcp via stdio.

Lazy-importa el SDK 'mcp' para que la clase pueda importarse sin instalarlo.
Solo `start()` levanta `McpNotInstalledError` si el paquete falta.

Requisitos de runtime (NO de pyproject):
  - Node.js >= 18 en PATH.
  - npx instalado (viene con npm/Node).
  - @playwright/mcp global o local en node_modules.

Esto se configura a nivel de imagen/Containerfile, no de pyproject.toml.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_SNAPSHOT_TOOL = "browser_snapshot"
_NAVIGATE_TOOL = "browser_navigate"
_CLICK_TOOL = "browser_click"
_TYPE_TOOL = "browser_type"
_PRESS_TOOL = "browser_press_key"
_SCREENSHOT_TOOL = "browser_take_screenshot"


class McpNotInstalledError(RuntimeError):
    """Paquete 'mcp' no esta instalado.

    Instala con: pip install 'hermes-runtime[browser-mcp]'
    """


class McpServerConnectionError(RuntimeError):
    """No se puede conectar al servidor @playwright/mcp via stdio."""


class StdioMcpSession:
    """Adaptador real sobre el servidor @playwright/mcp via MCP stdio.

    El servidor se lanza como subproceso via npx. La comunicacion ocurre
    sobre stdin/stdout del proceso hijo usando el protocolo JSON-RPC de MCP.

    Construccion:
        session = StdioMcpSession(
            server_command=["npx", "@playwright/mcp", "--headless"],
        )
        await session.start()   # lanza npx, conecta ClientSession

    Cierre:
        await session.close()   # desconecta ClientSession, mata el subproceso
    """

    def __init__(
        self,
        *,
        server_command: list[str] | None = None,
        timeout_sec: float = 30.0,
    ) -> None:
        if server_command is not None:
            self._server_command = server_command
        else:
            # Default @playwright/mcp launch. On the baked OS the bundled
            # Playwright Chromium is removed (dedup vs the system RPM); point the
            # MCP server at the system Chromium via env. Unset (dev/CI) → MCP uses
            # its own Playwright-managed browser unchanged.
            cmd = ["npx", "@playwright/mcp", "--headless"]
            chromium = os.environ.get("HERMES_CHROMIUM_EXECUTABLE", "")
            if chromium:
                cmd += ["--executable-path", chromium]
            self._server_command = cmd
        self._timeout_sec = timeout_sec
        # Lazy-initialized in start()
        self._client_session: Any = None
        self._stdio_context: Any = None
        self._closed = False

    async def start(self) -> None:
        """Lanza el servidor MCP y abre ClientSession.

        Raises:
            McpNotInstalledError: si el SDK 'mcp' no esta instalado.
            McpServerConnectionError: si el subproceso no arranca.
        """
        ClientSession, stdio_client = _import_mcp()
        try:
            from mcp.client.stdio import StdioServerParameters  # noqa: PLC0415

            params = StdioServerParameters(
                command=self._server_command[0],
                args=self._server_command[1:],
            )
            self._stdio_context = stdio_client(params)
            read_stream, write_stream = await self._stdio_context.__aenter__()
            self._client_session = ClientSession(read_stream, write_stream)
            await self._client_session.__aenter__()
            await self._client_session.initialize()
        except McpNotInstalledError:
            raise
        except Exception as exc:
            raise McpServerConnectionError(
                f"No se pudo conectar al servidor @playwright/mcp: {exc}"
            ) from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client_session is not None:
            try:
                await self._client_session.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes.browser.mcp_session_close_failed", extra={"error": str(exc)})
        if self._stdio_context is not None:
            try:
                await self._stdio_context.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hermes.browser.mcp_stdio_context_close_failed", extra={"error": str(exc)}
                )

    async def navigate(self, url: str) -> None:
        await self._call(_NAVIGATE_TOOL, {"url": url})

    async def snapshot(self) -> str:
        result = await self._call(_SNAPSHOT_TOOL, {})
        return _extract_text(result)

    async def click(self, ref: str) -> None:
        await self._call(_CLICK_TOOL, {"element": ref, "ref": ref})

    async def type_(self, ref: str, text: str) -> None:
        await self._call(_TYPE_TOOL, {"element": ref, "ref": ref, "text": text})

    async def press(self, key: str) -> None:
        await self._call(_PRESS_TOOL, {"key": key})

    async def current_url(self) -> str:
        # @playwright/mcp does not expose a direct "current_url" tool;
        # we read it from the snapshot header line "URL: <url>".
        snap = await self.snapshot()
        for line in snap.splitlines():
            if line.startswith("URL:") or line.startswith("url:"):
                return line.split(":", 1)[1].strip()
        return ""

    async def screenshot(self) -> bytes:
        result = await self._call(_SCREENSHOT_TOOL, {})
        # @playwright/mcp returns screenshot as base64 in content[0].data
        raw = _extract_text(result)
        try:
            return base64.b64decode(raw)
        except Exception:  # noqa: BLE001
            return raw.encode("latin-1")

    async def _call(self, tool: str, args: dict[str, Any]) -> Any:
        if self._client_session is None:
            raise McpServerConnectionError("StdioMcpSession.start() no fue llamado")
        return await self._client_session.call_tool(tool, args)


def _extract_text(result: Any) -> str:
    """Extrae texto plano de la respuesta de call_tool."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    # mcp SDK wraps in CallToolResult with .content list of TextContent
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None) or getattr(first, "data", None)
        if text is not None:
            return str(text)
    if isinstance(result, dict):
        return str(result.get("text", result.get("data", "")))
    return str(result)


def _import_mcp() -> tuple[Any, Any]:
    """Lazy-import del SDK mcp. Levanta McpNotInstalledError si falta."""
    try:
        from mcp import ClientSession  # noqa: PLC0415
        from mcp.client.stdio import stdio_client  # noqa: PLC0415

        return ClientSession, stdio_client
    except ImportError as exc:
        raise McpNotInstalledError(
            "El paquete 'mcp' no esta instalado. Instala con:\n"
            "    pip install 'hermes-runtime[browser-mcp]'\n"
            "El runtime tambien necesita Node.js y npx @playwright/mcp."
        ) from exc
