"""mcp/infrastructure/StdioMcpClient — generalized stdio MCP client.

This is the generalization of browser/infrastructure/mcp_session.py's
connection logic, extracted into the McpClientPort interface so that
any MCP server (not just @playwright/mcp) can be connected via stdio.

Decision (lift-option): COPY approach — StdioMcpClient is a NEW class
that reuses the same `mcp` SDK calls (same _import_mcp() helper, same
StdioServerParameters pattern). browser/infrastructure/mcp_session.py
is left COMPLETELY UNCHANGED (Liskov-safe by isolation). This is safer
because StdioMcpSession has 7 browser-specific methods (navigate,
snapshot, click, type_, press, current_url, screenshot) that are not
part of McpClientPort and would be misleading in the generalized type.
A thin wrapper/subclass would inherit all that browser state, violating
SRP. Creating a fresh class with the shared SDK idiom is lower-risk and
leaves the browser path byte-for-byte identical.

Requires (runtime, NOT pyproject):
  - mcp SDK installed (pip install mcp).
  - Node.js + npx for stdio servers that are npx-based.

Lazy-imports the mcp SDK so the module can be imported without it
installed (raises McpConnectionError only at initialize() time).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from hermes.mcp.application.errors import McpCallError, McpConnectionError
from hermes.mcp.domain.value_objects import Transport

logger = logging.getLogger("hermes.mcp.stdio_client")


class StdioMcpClient:
    """McpClientPort adapter for any MCP server launched via stdio.

    Args:
        transport: Transport.stdio([...]) specifying the command argv.
        timeout_sec: initialization timeout in seconds.
    """

    def __init__(self, transport: Transport, *, timeout_sec: float = 30.0) -> None:
        self._transport = transport
        self._timeout_sec = timeout_sec
        self._client_session: Any = None
        self._stdio_context: Any = None
        # Launcher path: raw fds to close on teardown; None on direct path.
        self._launcher_read_fd: int | None = None
        self._launcher_write_fd: int | None = None
        # Launcher path: pump tasks (stdout reader / stdin writer) + AsyncFile
        # wrappers, torn down on close.
        self._pump_tasks: list[Any] = []
        self._launcher_files: tuple[Any, Any] | None = None
        self._closed = False

    async def initialize(self) -> None:
        """Launch the subprocess and perform MCP handshake.

        When HERMES_MCP_LAUNCHER=1 (production), the subprocess is spawned by
        hermes-mcp-launcher outside the daemon's Landlock+seccomp sandbox, and
        its stdio fds are returned via SCM_RIGHTS. This avoids the "Connection
        closed" failure caused by MCP runners (uvx/npx/node) inheriting the
        daemon's confinement.

        When HERMES_MCP_LAUNCHER is absent or not "1" (CI/tests/direct mode),
        the original stdio_client() path is used unchanged.

        Raises:
            McpConnectionError: if the SDK is missing or subprocess fails.
        """
        argv = list(self._transport.argv)
        if os.environ.get("HERMES_MCP_LAUNCHER") == "1":
            await self._initialize_via_launcher(argv)
        else:
            await self._initialize_direct(argv)

    async def _initialize_via_launcher(self, argv: list[str]) -> None:
        """Launcher path: spawn via hermes-mcp-launcher, wire fds to ClientSession."""
        import asyncio as _asyncio  # noqa: PLC0415

        ClientSession = _import_mcp_session()

        from hermes.security.mcp_launcher_client import (  # noqa: PLC0415
            McpLauncherClient,
            McpLauncherError,
            McpLauncherUnavailable,
        )

        # Base env from the daemon unit (cache dirs, HOME, etc.).
        # BYOK overrides are merged on top: they have already been validated and
        # allowlist-checked by _validate_mcp_env in the infrastructure layer.
        # OD_API_TOKEN and similar secrets MUST NOT be logged — only the keys.
        env = _build_mcp_env()
        byok = dict(self._transport.env)
        if byok:
            env.update(byok)
            logger.debug(
                "hermes.mcp.stdio_client.byok_merged keys=%s", sorted(byok.keys())
            )
        launcher = McpLauncherClient()
        try:
            read_fd, write_fd, _pid = await launcher.spawn(argv=argv, env=env)
        except McpLauncherUnavailable as exc:
            raise McpConnectionError(
                f"StdioMcpClient: MCP launcher unavailable for {argv!r}: {exc}"
            ) from exc
        except McpLauncherError as exc:
            raise McpConnectionError(
                f"StdioMcpClient: MCP launcher error for {argv!r}: {exc}"
            ) from exc

        self._launcher_read_fd = read_fd
        self._launcher_write_fd = write_fd

        try:
            read_stream, write_stream = self._wire_launcher_streams(read_fd, write_fd)
            self._client_session = ClientSession(read_stream, write_stream)
            await self._client_session.__aenter__()
            # asyncio.timeout (same-task cancel scope) — see comment in direct path.
            async with _asyncio.timeout(self._timeout_sec):
                await self._client_session.initialize()
        except McpConnectionError:
            raise
        except Exception as exc:
            raise McpConnectionError(
                f"StdioMcpClient: handshake failed via launcher for {argv!r}: {exc}"
            ) from exc

    def _wire_launcher_streams(self, read_fd: int, write_fd: int) -> tuple[Any, Any]:
        """Replica el transporte stdio del SDK MCP sobre fds pre-existentes.

        El ClientSession NO acepta fds ni AsyncFile: espera un par de memory-object
        streams de SessionMessage (lo que stdio_client() construye internamente con
        anyio.open_process). Aquí lo replicamos desde los fds que el launcher pasó
        por SCM_RIGHTS: dos tareas pump (reader/writer) traducen bytes<->SessionMessage.

        - reader: lee bytes del fd de salida del MCP, decodifica UTF-8 incremental
          (multibyte partido entre lecturas), parte por líneas, valida cada línea
          como JSONRPCMessage y la envía como SessionMessage al read_stream.
        - writer: recibe SessionMessage del write_stream, serializa a JSON + '\\n'
          y lo escribe en el fd de entrada del MCP.

        Devuelve (read_stream, write_stream) tal como los espera ClientSession.
        Las tareas se guardan en self._pump_tasks y se cancelan en close().
        """
        import asyncio as _asyncio  # noqa: PLC0415
        import codecs  # noqa: PLC0415

        import anyio  # noqa: PLC0415
        from mcp.shared.message import SessionMessage  # noqa: PLC0415
        import mcp.types as _mcp_types  # noqa: PLC0415

        read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

        # closefd=False: los fileobj NO poseen los fds — el cierre único lo hace
        # close() con os.close(self._launcher_*_fd), evitando doble-cierre (y la
        # reutilización de fd que eso causaría).
        rf = anyio.wrap_file(os.fdopen(read_fd, "rb", buffering=0, closefd=False))
        wf = anyio.wrap_file(os.fdopen(write_fd, "wb", buffering=0, closefd=False))
        self._launcher_files = (rf, wf)

        async def _stdout_reader() -> None:
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            try:
                async with read_stream_writer:
                    buffer = ""
                    while True:
                        chunk = await rf.read(65536)
                        text = decoder.decode(chunk or b"", final=not chunk)
                        lines = (buffer + text).split("\n")
                        buffer = lines.pop()
                        for line in lines:
                            if not line:
                                continue
                            try:
                                message = _mcp_types.JSONRPCMessage.model_validate_json(line)
                            except Exception as exc:  # noqa: BLE001 — línea corrupta del MCP
                                await read_stream_writer.send(exc)
                                continue
                            await read_stream_writer.send(SessionMessage(message))
                        if not chunk:
                            break
            except (anyio.ClosedResourceError, _asyncio.CancelledError):
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes.mcp.launcher_reader_error: %s", exc)

        async def _stdin_writer() -> None:
            try:
                async with write_stream_reader:
                    async for session_message in write_stream_reader:
                        data = session_message.message.model_dump_json(
                            by_alias=True, exclude_none=True
                        )
                        await wf.write((data + "\n").encode("utf-8"))
                        await wf.flush()
            except (anyio.ClosedResourceError, _asyncio.CancelledError):
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes.mcp.launcher_writer_error: %s", exc)

        self._pump_tasks = [
            _asyncio.ensure_future(_stdout_reader()),
            _asyncio.ensure_future(_stdin_writer()),
        ]
        return read_stream, write_stream

    async def _initialize_direct(self, argv: list[str]) -> None:
        """Direct path: original stdio_client() spawn (used in CI/tests)."""
        ClientSession, stdio_client = _import_mcp()
        try:
            from mcp.client.stdio import (  # noqa: PLC0415
                StdioServerParameters,
                get_default_environment,
            )

            # El SDK pasa un env MÍNIMO al subproceso (get_default_environment),
            # que NO incluye npm_config_cache/UV_CACHE_DIR → npx/uvx caen a
            # $HOME/.npm (/var/home/hermes, con ficheros root-owned del bake) y
            # mueren con EACCES. Heredamos esos del env del daemon (los fija el
            # unit) para que el cache escribible viaje al runner MCP.
            env = dict(get_default_environment())
            for _k in (
                "npm_config_cache",
                "UV_CACHE_DIR",
                "npm_config_prefix",
                "HOME",
                "XDG_DATA_HOME",
                "XDG_CACHE_HOME",
                "UV_TOOL_DIR",
                "UV_PYTHON_INSTALL_DIR",
                "TMPDIR",
            ):
                _v = os.environ.get(_k)
                if _v:
                    env[_k] = _v
            # BYOK env (e.g. OD_DAEMON_URL for open-design-mcp); already validated.
            byok = dict(self._transport.env)
            if byok:
                env.update(byok)
            params = StdioServerParameters(command=argv[0], args=argv[1:], env=env)
            self._stdio_context = stdio_client(params)
            read_stream, write_stream = await self._stdio_context.__aenter__()
            self._client_session = ClientSession(read_stream, write_stream)
            await self._client_session.__aenter__()
            # timeout_sec se aplica con asyncio.timeout() (CM, MISMA task), NO con
            # asyncio.wait_for: wait_for ejecuta la corrutina en una task nueva, y
            # como stdio_client/ClientSession usan task-groups+cancel-scopes de
            # anyio entrados en ESTA task, el cruce de tasks lanzaba
            # "Attempted to exit cancel scope in a different task than it was
            # entered in" → "Connection closed" en TODOS los MCP. asyncio.timeout
            # cancela dentro de la misma task y preserva el scope.
            # (npx/uvx descargan el paquete entero en el primer arranque sin caché,
            # por eso el presupuesto debe ser amplio — 120s/300s desde el factory.)
            import asyncio as _asyncio  # noqa: PLC0415

            async with _asyncio.timeout(self._timeout_sec):
                await self._client_session.initialize()
        except McpConnectionError:
            raise
        except Exception as exc:
            raise McpConnectionError(
                f"StdioMcpClient: failed to connect via {argv!r}: {exc}"
            ) from exc

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return raw tool descriptors from the MCP server.

        Returns [] if the server exposes no tools; never raises on empty.
        Raises:
            McpConnectionError: if not initialized or transport down.
        """
        if self._client_session is None:
            raise McpConnectionError("StdioMcpClient.initialize() was not called")
        try:
            result = await self._client_session.list_tools()
            tools = getattr(result, "tools", None) or []
            return [_tool_to_dict(t) for t in tools]
        except McpConnectionError:
            raise
        except Exception as exc:
            raise McpConnectionError(f"StdioMcpClient.list_tools failed: {exc}") from exc

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool and return the result as a dict.

        Raises:
            McpConnectionError: if not initialized.
            McpCallError: if the server returns a protocol-level error.
        """
        if self._client_session is None:
            raise McpConnectionError("StdioMcpClient.initialize() was not called")
        try:
            result = await self._client_session.call_tool(name, args)
            return _extract_result(result)
        except (McpConnectionError, McpCallError):
            raise
        except Exception as exc:
            raise McpCallError(
                f"StdioMcpClient.call_tool({name!r}) failed: {exc}"
            ) from exc

    async def close(self) -> None:
        """Tear down gracefully. Idempotent.

        On the launcher path: closing the ClientSession causes the MCP child to
        receive EOF on its stdin and exit naturally. The launcher reaps it via
        SIGCHLD/waitpid. We then close our fd copies to free kernel resources.
        """
        if self._closed:
            return
        self._closed = True
        # ── Launcher path: orden de teardown determinista ──────────────────────
        # 1) Cancelar las tareas pump (dejan de empujar a los memory-streams).
        # 2) Cerrar write_fd PRIMERO = stdin del MCP → EOF → el hijo sale solo (el
        #    launcher lo reapea por SIGCHLD). 3) Cerrar read_fd = stdout del MCP →
        #    desbloquea el read() del reader que pueda seguir bloqueado en su hilo
        #    (devuelve EOF/EBADF y la tarea acaba). Hacerlo ANTES del cierre del
        #    ClientSession evita que su task-group se quede esperando al transporte.
        for _task in self._pump_tasks:
            try:
                _task.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._pump_tasks = []
        self._launcher_files = None
        for fd in (self._launcher_write_fd, self._launcher_read_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._launcher_read_fd = None
        self._launcher_write_fd = None
        # Ahora cerrar la sesión MCP y, en el camino directo, el stdio_context.
        if self._client_session is not None:
            try:
                await self._client_session.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes.mcp.stdio_client.session_close_error: %s", exc)
        if self._stdio_context is not None:
            try:
                await self._stdio_context.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes.mcp.stdio_client.stdio_context_close_error: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_mcp() -> tuple[Any, Any]:
    """Lazy-import of the mcp SDK. Raises McpConnectionError if not installed."""
    try:
        from mcp import ClientSession  # noqa: PLC0415
        from mcp.client.stdio import stdio_client  # noqa: PLC0415

        return ClientSession, stdio_client
    except ImportError as exc:
        raise McpConnectionError(
            "The 'mcp' package is not installed. Install with:\n"
            "    pip install 'hermes-runtime[browser-mcp]'\n"
        ) from exc


def _import_mcp_session() -> Any:
    """Lazy-import only ClientSession. Raises McpConnectionError if not installed."""
    try:
        from mcp import ClientSession  # noqa: PLC0415

        return ClientSession
    except ImportError as exc:
        raise McpConnectionError(
            "The 'mcp' package is not installed. Install with:\n"
            "    pip install 'hermes-runtime[browser-mcp]'\n"
        ) from exc


def _build_mcp_env() -> dict[str, str]:
    """Build the env dict to forward to the launcher (whitelisted keys only)."""
    keys = (
        "npm_config_cache",
        "UV_CACHE_DIR",
        "npm_config_prefix",
        "HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "UV_TOOL_DIR",
        "UV_PYTHON_INSTALL_DIR",
        # uvx populates its cache by hard-linking/renaming the built sdist; under
        # the launcher's systemd sandbox (ProtectSystem=strict + ReadWritePaths
        # bind mounts) that crosses a mount boundary → "Invalid cross-device link"
        # → the MCP dies before the handshake. UV_LINK_MODE=copy makes uv copy
        # instead, which is sandbox-safe. Must be in this allowlist or the unit's
        # value never reaches the subprocess.
        "UV_LINK_MODE",
        "TMPDIR",
        "PATH",
    )
    return {k: v for k in keys if (v := os.environ.get(k))}


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    """Convert an mcp SDK Tool object to a plain dict."""
    annotations = getattr(tool, "annotations", None)
    annotations_dict: dict[str, Any] = {}
    if annotations is not None:
        annotations_dict = {
            "readOnlyHint": getattr(annotations, "readOnlyHint", None),
            "destructiveHint": getattr(annotations, "destructiveHint", None),
        }
    return {
        "name": getattr(tool, "name", ""),
        "description": getattr(tool, "description", ""),
        "inputSchema": getattr(tool, "inputSchema", {}),
        "annotations": annotations_dict,
    }


def _extract_result(result: Any) -> dict[str, Any]:
    """Extract a plain dict from an mcp SDK CallToolResult."""
    if isinstance(result, dict):
        return result
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None) or getattr(first, "data", None)
        if text is not None:
            return {"content": str(text)}
    return {"content": str(result)}
