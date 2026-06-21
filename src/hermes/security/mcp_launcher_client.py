"""McpLauncherClient — daemon-side async AF_UNIX client to hermes-mcp-launcher.

Runs inside hermes-runtime.service (confined hermes daemon) and communicates
with hermes-mcp-launcher.service (unprivileged helper running outside the
Landlock+seccomp sandbox) over the AF_UNIX socket /run/hermes/mcp-launch.sock.

The socket is hermes:hermes 0660. The daemon (User=hermes) is the owner and
connects with that uid/gid. The launcher validates SO_PEERCRED
(gid == hermes); no additional token is needed.

Wire protocol:
  Request  (send):     4-byte BE length + UTF-8 JSON
      {"argv": ["uvx", "..."], "env": {"KEY": "VAL", ...}}
  Response (recvmsg):  4-byte BE length + UTF-8 JSON  (normal data)
                       + SCM_RIGHTS ancillary with [read_fd, write_fd]
      read_fd  = read-end of MCP stdout pipe (daemon reads JSON-RPC replies)
      write_fd = write-end of MCP stdin pipe (daemon writes JSON-RPC requests)

This client sends ONE request, reads ONE response, closes. Stateless.

Mirrors BrowserLauncherClient (same error types, same fail-soft pattern).
Capa: infrastructure (security / privilege bridge).
"""

from __future__ import annotations

import array
import asyncio
import json
import logging
import os
import socket
import struct
from pathlib import Path

logger = logging.getLogger("hermes.security.mcp_launcher_client")

# /run/hermes-mcp (not /run/hermes): the launcher runs as hermes-sandbox, which is
# NOT in group `hermes` and so cannot create a socket in /run/hermes (0770 hermes:hermes).
# Its own dir keeps the agent's MCP children from tampering /run/hermes' control sockets.
# (Aislamiento /proc/fd 2026-06-19.)
_SOCKET_PATH = Path("/run/hermes-mcp/launch.sock")
_CONNECT_TIMEOUT_S: float = 5.0
_IO_TIMEOUT_S: float = 30.0
_MAX_FRAME_BYTES: int = 32 * 1024
# Number of file descriptors expected in the SCM_RIGHTS ancillary: [read_fd, write_fd]
_FDS_COUNT: int = 2


class McpLauncherUnavailable(RuntimeError):
    """The MCP launcher helper is not reachable (unit not started or socket missing)."""


class McpLauncherError(RuntimeError):
    """The launcher returned ok=False or sent an unexpected response."""


class McpLauncherClient:
    """Async client for hermes-mcp-launcher.

    Usage:
        client = McpLauncherClient()
        read_fd, write_fd, pid = await client.spawn(
            argv=["uvx", "--from", "...", "serena", "start-mcp-server"],
            env={"UV_CACHE_DIR": "/var/lib/hermes/uv-cache", ...},
        )
        # read_fd = MCP stdout (daemon reads JSON-RPC from here)
        # write_fd = MCP stdin  (daemon writes JSON-RPC here)

    Raises McpLauncherUnavailable when the socket is absent or unreachable.
    Raises McpLauncherError when the launcher returns ok=False.
    """

    def __init__(self, *, socket_path: Path = _SOCKET_PATH) -> None:
        self._socket_path = socket_path

    async def spawn(
        self,
        *,
        argv: list[str],
        env: dict[str, str],
    ) -> tuple[int, int, int]:
        """Request the launcher to spawn an MCP server subprocess.

        Args:
            argv: Full command vector. argv[0] must be in the launcher allowlist
                  {npx, uvx, node, python3}; validated server-side.
            env:  Environment overrides. Only whitelisted keys are forwarded;
                  the launcher base env comes from its own unit environment.

        Returns:
            (read_fd, write_fd, pid)
              read_fd:  daemon reads MCP JSON-RPC replies from this fd.
              write_fd: daemon writes MCP JSON-RPC requests to this fd.
              pid:      PID of the spawned MCP process (for logging/monitoring).

        Raises:
            McpLauncherUnavailable: socket absent, connection refused, or timeout.
            McpLauncherError: launcher returned ok=False or fd count mismatch.
        """
        request = {"argv": list(argv), "env": dict(env)}
        read_fd, write_fd, pid = await self._roundtrip(request)
        logger.info(
            "mcp_launcher_client.spawned argv=%s pid=%d read_fd=%d write_fd=%d",
            argv[0] if argv else "",
            pid,
            read_fd,
            write_fd,
        )
        return read_fd, write_fd, pid

    async def _roundtrip(self, request: dict) -> tuple[int, int, int]:
        if not self._socket_path.exists():
            raise McpLauncherUnavailable(
                f"MCP launcher socket not found: {self._socket_path}"
            )

        # recvmsg with SCM_RIGHTS is not supported by asyncio high-level streams;
        # we use a raw socket + run_in_executor for the receive step.
        # The full roundtrip is short (one send/recv), so the executor overhead
        # is negligible and the code stays straightforward.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(True)
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, sock.connect, str(self._socket_path)),
                timeout=_CONNECT_TIMEOUT_S,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            sock.close()
            raise McpLauncherUnavailable(
                f"Cannot connect to MCP launcher at {self._socket_path}: {exc}"
            ) from exc

        try:
            body = json.dumps(request).encode("utf-8")
            frame = struct.pack(">I", len(body)) + body

            await asyncio.wait_for(
                loop.run_in_executor(None, sock.sendall, frame),
                timeout=_IO_TIMEOUT_S,
            )

            read_fd, write_fd, pid = await asyncio.wait_for(
                loop.run_in_executor(None, _recv_fds_and_frame, sock),
                timeout=_IO_TIMEOUT_S,
            )
        except asyncio.TimeoutError as exc:
            raise McpLauncherUnavailable("MCP launcher I/O timeout") from exc
        finally:
            sock.close()

        return read_fd, write_fd, pid


# ── Low-level SCM_RIGHTS receive (runs in executor, not in event loop) ─────────


def _recv_fds_and_frame(sock: socket.socket) -> tuple[int, int, int]:
    """Receive the launcher's response: JSON frame + SCM_RIGHTS fds.

    Runs synchronously in a thread-pool executor (called via run_in_executor).
    Returns (read_fd, write_fd, pid).

    The SCM_RIGHTS ancillary carries exactly _FDS_COUNT fds: [stdout_read, stdin_write].
    """
    fds_buf = array.array("i")
    ancbuf_size = socket.CMSG_LEN(_FDS_COUNT * fds_buf.itemsize)

    msg, ancdata, _flags, _addr = sock.recvmsg(4 + _MAX_FRAME_BYTES, ancbuf_size)
    if len(msg) < 4:
        raise McpLauncherError("MCP launcher closed connection without response")

    received_fds: list[int] = []
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            chunk = array.array("i")
            # Truncate to a multiple of itemsize to avoid partial-int corruption.
            aligned_len = len(cmsg_data) - (len(cmsg_data) % chunk.itemsize)
            chunk.frombytes(cmsg_data[:aligned_len])
            received_fds.extend(int(fd) for fd in chunk)

    resp = _parse_frame(msg)

    if not resp.get("ok", False):
        for fd in received_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        raise McpLauncherError(
            f"MCP launcher returned error: {resp.get('error', 'unknown')}"
        )

    if len(received_fds) != _FDS_COUNT:
        for fd in received_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        raise McpLauncherError(
            f"MCP launcher sent {len(received_fds)} fds; expected {_FDS_COUNT}"
        )

    read_fd, write_fd = received_fds[0], received_fds[1]
    # Ensure fds are not inherited by further subprocesses spawned by the daemon.
    os.set_inheritable(read_fd, False)
    os.set_inheritable(write_fd, False)

    pid = int(resp.get("pid", 0))
    return read_fd, write_fd, pid


def _parse_frame(data: bytes) -> dict:
    """Parse a 4-byte BE length-prefixed JSON frame from raw recvmsg data."""
    if len(data) < 4:
        raise McpLauncherError("response frame too short")
    length = struct.unpack(">I", data[:4])[0]
    if length > _MAX_FRAME_BYTES:
        raise McpLauncherError(f"response frame too large: {length}")
    body = data[4 : 4 + length]
    return json.loads(body.decode("utf-8"))
