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
        # Session ownership: the MCP ClientSession (and, on the direct path, the
        # stdio_client context) enter anyio cancel scopes bound to the TASK that
        # enters them. If teardown runs in a different task (disconnect handler),
        # anyio raises "Attempted to exit cancel scope in a different task". So a
        # single long-lived owner task enters AND exits the session; close() just
        # signals it. See _session_owner.
        self._owner_task: Any = None
        self._close_evt: Any = None

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
        # Run the PREFETCHED bin directly instead of `npx --offline <pkg>` (which fails
        # ENOTCACHED — see offline_runtime.py). Pure rewrite; passes through unchanged
        # for non-prefetched / non-npx argv, so CI/direct mode is unaffected.
        from hermes.mcp.infrastructure.offline_runtime import (  # noqa: PLC0415
            resolve_runtime_argv,
        )
        argv = resolve_runtime_argv(list(self._transport.argv))
        if os.environ.get("HERMES_MCP_LAUNCHER") == "1":
            await self._initialize_via_launcher(argv)
        else:
            await self._initialize_direct(argv)

    async def _initialize_via_launcher(self, argv: list[str]) -> None:
        """Launcher path: spawn via hermes-mcp-launcher, wire fds to ClientSession."""
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
            # Enter/exit the ClientSession inside ONE owner task (same-task cancel
            # scope). Blocks until the handshake completes or raises.
            await self._start_session_owner(streams=(read_stream, write_stream))
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
        _ClientSession, stdio_client = _import_mcp()
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
            # Own BOTH the stdio_client context AND the ClientSession in ONE task,
            # so their anyio cancel scopes are entered and exited in the same task
            # (see _session_owner). Blocks until the handshake completes or raises.
            await self._start_session_owner(stdio_ctx=stdio_client(params))
        except McpConnectionError:
            raise
        except Exception as exc:
            raise McpConnectionError(
                f"StdioMcpClient: failed to connect via {argv!r}: {exc}"
            ) from exc

    async def _start_session_owner(
        self, *, streams: tuple[Any, Any] | None = None, stdio_ctx: Any = None
    ) -> None:
        """Spawn the owner task that holds the MCP session for its whole life.

        Blocks until the session is initialized (returns) or the handshake fails
        (raises the original exception). The owner task then idles until close()
        signals it, at which point it exits the session IN ITS OWN TASK — so the
        anyio cancel scope is always exited in the task that entered it.
        """
        import asyncio as _asyncio  # noqa: PLC0415

        self._close_evt = _asyncio.Event()
        ready: Any = _asyncio.get_event_loop().create_future()
        self._owner_task = _asyncio.ensure_future(
            self._session_owner(ready, streams=streams, stdio_ctx=stdio_ctx)
        )
        await ready  # propagates the connect error, if any

    async def _session_owner(
        self, ready: Any, *, streams: tuple[Any, Any] | None = None, stdio_ctx: Any = None
    ) -> None:
        """Enter → initialize → serve → exit the MCP session, all in THIS task."""
        import asyncio as _asyncio  # noqa: PLC0415

        try:
            if stdio_ctx is not None:
                async with stdio_ctx as (read_stream, write_stream):
                    await self._serve_session(read_stream, write_stream, ready)
            else:
                assert streams is not None
                await self._serve_session(streams[0], streams[1], ready)
        except _asyncio.CancelledError:
            # close() cancelled us as a fallback; the async-with teardown above
            # already ran the session __aexit__ in this task. Nothing to log.
            pass
        except Exception as exc:  # noqa: BLE001
            if not ready.done():
                ready.set_exception(exc)
            else:
                # Teardown-time hiccup AFTER a healthy session — cosmetic; DEBUG only.
                logger.debug("hermes.mcp.stdio_client.session_owner_exit: %s", exc)
        finally:
            self._client_session = None

    async def _serve_session(self, read_stream: Any, write_stream: Any, ready: Any) -> None:
        """Enter the ClientSession, initialize, publish it, and idle until close."""
        import asyncio as _asyncio  # noqa: PLC0415

        ClientSession = _import_mcp_session()
        async with ClientSession(read_stream, write_stream) as session:
            try:
                async with _asyncio.timeout(self._timeout_sec):
                    await session.initialize()
            except BaseException as exc:  # noqa: BLE001 — surface to the connect caller
                if not ready.done():
                    ready.set_exception(exc)
                raise  # exit the async-with → __aexit__ runs in THIS task
            self._client_session = session
            if not ready.done():
                ready.set_result(True)
            # Hold the session open until close() asks us to tear it down.
            await self._close_evt.wait()

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

        The MCP session (ClientSession, and on the direct path the stdio_client
        context) is owned by _session_owner. We SIGNAL that task to exit — so the
        anyio cancel scopes are exited in the SAME task that entered them (no
        "exit cancel scope in a different task" warning) — then reap our launcher
        transport (pump tasks + fd copies). Closing the session makes the MCP
        child receive EOF on stdin and exit; the launcher reaps it via SIGCHLD.
        """
        import asyncio as _asyncio  # noqa: PLC0415

        if self._closed:
            return
        self._closed = True
        # 1) Ask the owner task to exit the session in ITS OWN task, then join it.
        if self._close_evt is not None:
            self._close_evt.set()
        if self._owner_task is not None:
            try:
                await _asyncio.wait_for(self._owner_task, timeout=15.0)
            except _asyncio.TimeoutError:
                self._owner_task.cancel()
                try:
                    await self._owner_task
                except (Exception, _asyncio.CancelledError):  # noqa: BLE001
                    pass
            except (Exception, _asyncio.CancelledError):  # noqa: BLE001
                pass
            self._owner_task = None
        # 2) Reap the launcher transport (no-op on the direct path). Cancel the
        #    pump tasks and close our fd copies to free kernel resources.
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
        self._client_session = None
        self._stdio_context = None


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
