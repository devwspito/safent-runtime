"""WS /api/v1/chat/stream/{task_id} — AF_UNIX → browser WebSocket bridge.

Browsers cannot connect to AF_UNIX sockets, so this endpoint relays the daemon's
task stream (AF_UNIX /run/hermes/tasks.sock, path /ws/tasks/{id}) to the browser.

Why high-level asyncio streams (not TaskStreamClient): the shell-server runs
under uvicorn + **uvloop**, whose raw ``loop.sock_recv``/``loop.sock_connect``
path used by TaskStreamClient yields no frames (verified: the identical client
streams perfectly on the stdlib loop but returns empty under uvloop). We connect
with ``asyncio.open_unix_connection`` — which works identically on uvloop and the
stdlib loop — and reuse the daemon's own handshake builder + WS frame parser so
the wire protocol stays byte-for-byte the proven one. Frames are forwarded
VERBATIM (the daemon already emits valid {kind, task_id, ...} JSON).

Security: stream_path is derived deterministically from the validated UUID
(/ws/tasks/{uuid}) — no client input shapes the socket path. The AF_UNIX
connection is the shell-server process (uid hermes), authorised by the daemon's
SO_PEERCRED check. No payloads are logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from hermes.shell.infrastructure.dbus_fast_runtime_client import (
    StreamFrame,
    TaskStreamClient,
    _build_ws_handshake,
    _parse_ws_frame,
)

logger = logging.getLogger("hermes.shell_server.cowork.chat_stream")

_SOCKET_PATH: str = TaskStreamClient.SOCKET_PATH
_STREAM_TIMEOUT_S: float = 600.0          # hard cap; browser reconnects if longer
_HANDSHAKE_RETRIES: int = 4               # stream may not be registered the instant we connect
_HANDSHAKE_RETRY_DELAY_S: float = 0.4


def create_chat_stream_router() -> APIRouter:
    router = APIRouter()

    @router.websocket("/api/v1/chat/stream/{task_id}")
    async def chat_stream(websocket: WebSocket, task_id: str) -> None:
        await websocket.accept()

        parsed = _parse_task_id(task_id)
        if parsed is None:
            await _close_with_error(websocket, "invalid_task_id", task_id)
            return

        stream_path = f"/ws/tasks/{parsed}"
        try:
            await asyncio.wait_for(
                _relay(websocket, stream_path, str(parsed)),
                timeout=_STREAM_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            await _close_with_error(websocket, "stream_timeout", str(parsed))
        except WebSocketDisconnect:
            logger.debug("hermes.cowork.chat_stream.browser_disconnected")
        except Exception as exc:  # noqa: BLE001 — never let one stream crash the worker
            logger.warning(
                "hermes.cowork.chat_stream.error",
                extra={"error": str(exc)},
            )
            await _close_with_error(websocket, str(exc), str(parsed))

    return router


# ---------------------------------------------------------------------------


def _parse_task_id(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        return None


async def _relay(websocket: WebSocket, stream_path: str, task_id: str) -> None:
    """Connect to the daemon stream socket and forward frames to the browser."""
    reader, writer = await _connect_with_handshake(stream_path)
    try:
        buf = b""
        while True:
            try:
                _fin, opcode, payload, buf = _parse_ws_frame(buf)
            except ValueError:  # need more bytes
                chunk = await reader.read(65536)
                if not chunk:
                    break  # daemon EOF → stream done
                buf += chunk
                continue

            if opcode == 0x8:  # close
                break
            if opcode == 0x9:  # ping → pong (unmasked; daemon tolerates it, as proven)
                writer.write(bytes([0x8A, 0x00]))
                await writer.drain()
                continue
            if opcode in (0x1, 0x2) and payload:
                text = payload.decode("utf-8", errors="replace")
                # Forward verbatim — the daemon already emits {kind, task_id, ...}.
                await websocket.send_text(text)
                if _is_terminal(text):
                    break
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    try:
        await websocket.close()
    except Exception:  # noqa: BLE001
        pass


async def _connect_with_handshake(stream_path: str):
    """Open the AF_UNIX socket and complete the WS upgrade. Retries briefly if the
    stream is not yet registered (task just enqueued)."""
    last_exc: Exception | None = None
    for attempt in range(_HANDSHAKE_RETRIES):
        try:
            reader, writer = await asyncio.open_unix_connection(path=_SOCKET_PATH)
            writer.write(_build_ws_handshake(stream_path))
            await writer.drain()
            headers = b""
            while b"\r\n\r\n" not in headers:
                chunk = await reader.read(4096)
                if not chunk:
                    raise ConnectionError("socket closed during WS handshake")
                headers += chunk
            return reader, writer
        except (OSError, ConnectionError) as exc:
            last_exc = exc
            await asyncio.sleep(_HANDSHAKE_RETRY_DELAY_S * (attempt + 1))
    raise ConnectionError(f"daemon stream handshake failed: {last_exc}")


def _is_terminal(text: str) -> bool:
    try:
        return StreamFrame.from_json(text).kind in ("done", "error")
    except Exception:  # noqa: BLE001
        return '"kind":"done"' in text or '"kind": "done"' in text


async def _close_with_error(websocket: WebSocket, reason: str, task_id: str) -> None:
    frame = json.dumps(
        {"kind": "error", "task_id": task_id, "protocol_version": 1, "error": reason},
        separators=(",", ":"),
    )
    try:
        await websocket.send_text(frame)
        await websocket.close()
    except Exception:  # noqa: BLE001
        pass
