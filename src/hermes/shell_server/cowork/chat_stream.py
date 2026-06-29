"""SSE /api/v1/chat/stream/{task_id} — AF_UNIX → browser Server-Sent Events bridge.

WHY SSE (not WebSocket): SSE is the LLM-streaming protocol (what OpenAI/Anthropic
use) and, crucially, it has NATIVE resumability. The browser's EventSource auto-
reconnects on any drop and re-sends `Last-Event-ID`; we use the daemon's per-task
monotonic `seq` as the SSE event `id`, so on reconnect we resume from exactly where
the client left off (replaying only the missed frames from the broker's replay log).
This makes a mid-stream page refresh resilient by PROTOCOL — the bespoke WebSocket
reconnect/replay code (the source of the recurring "chat dies on refresh" bug) is
gone. There is also no wall-clock cap: the stream lives as long as frames flow.

The daemon side is unchanged: we still connect to the AF_UNIX task stream socket
(/run/hermes/tasks.sock, path /ws/tasks/{id}) which speaks the proven WS frame
protocol, parse those frames, and re-emit them VERBATIM as SSE `data:` events.

Why high-level asyncio streams: the shell-server runs under uvicorn+uvloop, whose
raw loop.sock_recv path used by TaskStreamClient yields no frames; asyncio.open_unix_
connection works identically on uvloop and the stdlib loop, so we use it + reuse the
daemon's handshake builder + WS frame parser.

Security: stream_path is derived deterministically from the validated UUID — no
client input shapes the socket path. The endpoint is same-origin loopback + an
unguessable task UUID (the WS endpoint was likewise unauthenticated); GET is not
gated by the operator-token middleware (mutations-only) and /api/v1/chat is in the
feature-guard always-allowed set. No payloads are logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from hermes.shell.infrastructure.dbus_fast_runtime_client import (
    StreamFrame,
    TaskStreamClient,
    _build_ws_handshake,
    _parse_ws_frame,
)

logger = logging.getLogger("hermes.shell_server.cowork.chat_stream")

_SOCKET_PATH: str = TaskStreamClient.SOCKET_PATH
# IDLE keepalive, NOT a wall-clock cap. Every this-many seconds of NO frames we emit
# an SSE comment to keep proxies (nginx/tailscale funnel) and the browser warm. The
# stream closes only after _MAX_IDLE_S of TOTAL silence (a genuinely stalled task) —
# and even then EventSource reconnects and we replay from Last-Event-ID.
_IDLE_READ_TIMEOUT_S: float = 25.0
_MAX_IDLE_S: float = 300.0
_HANDSHAKE_RETRIES: int = 4               # stream may not be registered the instant we connect
_HANDSHAKE_RETRY_DELAY_S: float = 0.4

# SSE response headers: no caching, and disable proxy buffering so tokens flush live.
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def create_chat_stream_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/chat/stream/{task_id}")
    async def chat_stream(task_id: str, request: Request) -> StreamingResponse:
        parsed = _parse_task_id(task_id)
        if parsed is None:
            return StreamingResponse(
                _single_error_event(task_id, "invalid_task_id"),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )
        # Resume handle: EventSource re-sends the last `id:` it saw as the
        # `Last-Event-ID` header on reconnect. (Some setups also pass ?lastEventId=.)
        last_event_id = _parse_last_event_id(
            request.headers.get("last-event-id")
            or request.query_params.get("lastEventId")
        )
        stream_path = f"/ws/tasks/{parsed}"
        return StreamingResponse(
            _sse_relay(stream_path, str(parsed), last_event_id, request),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    return router


# ---------------------------------------------------------------------------


def _parse_task_id(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        return None


def _parse_last_event_id(raw: str | None) -> int:
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


async def _sse_relay(
    stream_path: str, task_id: str, last_event_id: int, request: Request
) -> AsyncIterator[str]:
    """Connect to the daemon task stream and yield SSE events to the browser.

    Frames already seen (seq <= last_event_id) are skipped, so a reconnecting client
    resumes without duplicates. `id:` carries the seq for the browser to echo back.
    """
    try:
        reader, writer = await _connect_with_handshake(stream_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes.cowork.chat_stream.connect_failed", extra={"error": str(exc)})
        yield _sse_error_frame(task_id, str(exc))
        return

    idle_total = 0.0
    try:
        buf = b""
        while True:
            if await request.is_disconnected():
                break
            try:
                _fin, opcode, payload, buf = _parse_ws_frame(buf)
            except ValueError:  # need more bytes
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(65536), timeout=_IDLE_READ_TIMEOUT_S
                    )
                except asyncio.TimeoutError:
                    idle_total += _IDLE_READ_TIMEOUT_S
                    if idle_total >= _MAX_IDLE_S:
                        logger.info(
                            "hermes.cowork.chat_stream.idle_close",
                            extra={"task_id": task_id},
                        )
                        break  # stalled → close; EventSource reconnects + we replay
                    yield ": keepalive\n\n"  # SSE comment — ignored by EventSource
                    continue
                if not chunk:
                    break  # daemon EOF → stream done
                idle_total = 0.0
                buf += chunk
                continue

            if opcode == 0x8:  # close
                break
            if opcode == 0x9:  # ping → pong (unmasked; daemon tolerates it)
                writer.write(bytes([0x8A, 0x00]))
                await writer.drain()
                continue
            if opcode in (0x1, 0x2) and payload:
                text = payload.decode("utf-8", errors="replace")
                seq = _frame_seq(text)
                if seq is not None and seq <= last_event_id:
                    continue  # already delivered before the reconnect → resume skip
                if seq is not None:
                    yield f"id: {seq}\ndata: {text}\n\n"
                else:
                    yield f"data: {text}\n\n"
                if _is_terminal(text):
                    break
    finally:
        writer.close()
        try:
            await writer.wait_closed()
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


def _frame_seq(text: str) -> int | None:
    """Extract the monotonic per-task seq from a serialized frame (top-level, since
    to_jsonl flattens payload; fall back to payload.seq for safety)."""
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    s = obj.get("seq")
    if s is None and isinstance(obj.get("payload"), dict):
        s = obj["payload"].get("seq")
    try:
        return int(s) if s is not None else None
    except (TypeError, ValueError):
        return None


def _is_terminal(text: str) -> bool:
    try:
        return StreamFrame.from_json(text).kind in ("done", "error")
    except Exception:  # noqa: BLE001
        return '"kind":"done"' in text or '"kind": "done"' in text


def _sse_error_frame(task_id: str, reason: str) -> str:
    frame = json.dumps(
        {"kind": "error", "task_id": task_id, "protocol_version": 1, "error": reason},
        separators=(",", ":"),
    )
    return f"data: {frame}\n\n"


async def _single_error_event(task_id: str, reason: str) -> AsyncIterator[str]:
    yield _sse_error_frame(task_id, reason)
