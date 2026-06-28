"""DbusRuntimeClient — proxy D-Bus org.hermes.Runtime1 (feature 006, T053).

Implements AgentRuntimePort (application layer port) via dbus-fast async.
Also provides TaskStreamClient that subscribes to the AF_UNIX task stream
socket returned by Enqueue.stream_path (/ws/tasks/{task_id}).

Design contracts (SRP):
- NO GTK / GLib / gi.repository in this module (headless testable).
- Pure asyncio: `dbus-fast` async client + websockets AF_UNIX.
- Wire protocol follows dbus_runtime_iface_v1.md + task_stream_socket_v1.md.
- Auth: D-Bus UID resolution happens server-side; client sends no uid param.
- Testable with FakeDbusInterface + FakeTaskSocketServer (no real bus in CI).
  Tests that require a real D-Bus system bus or a running daemon are marked
  `requires_vm`.

Public surface:
  DbusRuntimeClient     — implements AgentRuntimePort.get_status / Enqueue / Pause / Resume
  TaskStreamClient      — subscribes to /ws/tasks/{task_id} over AF_UNIX
  FakeDbusInterface     — in-memory fake for unit tests (no bus required)
  FakeTaskStreamServer  — in-memory fake that yields pre-configured frames
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

logger = logging.getLogger("hermes.shell.infrastructure.dbus_runtime_client")

# ---------------------------------------------------------------------------
# Domain types — task stream frames (matches task_stream_socket_v1.md)
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = 1

_VALID_STREAM_KINDS = frozenset(
    {"delta", "thinking_delta", "tool_call", "status", "done", "error"}
)


@dataclass(frozen=True, slots=True)
class StreamFrame:
    """Parsed frame from the task stream socket.

    Mirrors TaskStreamFrame from task_stream_socket_v1.md but lives in the
    shell infrastructure layer so the GTK layer can import it without pulling
    in the full task domain.
    """

    kind: str
    task_id: str
    payload: dict[str, Any]
    protocol_version: int = PROTOCOL_VERSION

    @classmethod
    def from_json(cls, raw: str) -> "StreamFrame":
        """Deserialise a wire frame. Raises ValueError on schema violations."""
        data = json.loads(raw)
        for required in ("kind", "task_id", "protocol_version"):
            if required not in data:
                raise ValueError(f"StreamFrame missing required field: {required!r}")
        kind = data["kind"]
        if kind not in _VALID_STREAM_KINDS:
            raise ValueError(f"StreamFrame unknown kind: {kind!r}")
        payload = {k: v for k, v in data.items() if k not in ("kind", "task_id", "protocol_version")}
        return cls(
            kind=kind,
            task_id=data["task_id"],
            payload=payload,
            protocol_version=data["protocol_version"],
        )


# ---------------------------------------------------------------------------
# Port: task stream subscription
# ---------------------------------------------------------------------------


class TaskStreamPort:
    """Async iterator interface for a single-task stream subscription.

    Concrete adapters: TaskStreamClient (AF_UNIX websocket) and
    FakeTaskStreamServer (in-memory for tests).
    """

    async def frames(self) -> AsyncIterator[StreamFrame]:
        """Yield StreamFrames until the stream ends or the connection is cut."""
        raise NotImplementedError  # pragma: no cover
        yield  # make this a generator type  # noqa: unreachable


# ---------------------------------------------------------------------------
# FakeDbusInterface — in-memory fake (no bus required, CI-safe)
# ---------------------------------------------------------------------------


@dataclass
class EnqueueResult:
    task_id: str
    stream_path: str


@dataclass
class QueueStatus:
    state: str
    pending: int
    in_progress: int
    pending_approval: int
    last_audit_head: str


class FakeDbusInterface:
    """In-memory replacement for the real D-Bus proxy.

    Usage in tests:
        fake = FakeDbusInterface()
        fake.queue_status_response(state="idle", pending=0, in_progress=0)
        fake.queue_enqueue_result(task_id="abc", stream_path="/ws/tasks/abc")
        client = DbusRuntimeClient(dbus_interface=fake)
        status = await client.get_status()

    call_Enqueue accepts 5 positional args (trigger_kind, text, priority,
    dedup_key, conversation_id) so the Fake stays compatible with
    DbusRuntimeClient after the 5-arg change.
    """

    def __init__(self) -> None:
        self._status_queue: list[dict | Exception] = []
        self._enqueue_queue: list[EnqueueResult | Exception] = []
        self._pause_results: list[bool | Exception] = []
        self._resume_results: list[bool | Exception] = []
        self.enqueue_calls: list[dict] = []
        self.pause_calls: list[str] = []
        self.resume_calls: int = 0

    # Queue helpers --------------------------------------------------------

    def queue_status_response(
        self,
        *,
        state: str = "idle",
        pending: int = 0,
        in_progress: int = 0,
        pending_approval: int = 0,
        last_audit_head: str = "",
    ) -> None:
        self._status_queue.append(
            {
                "state": state,
                "pending": pending,
                "in_progress": in_progress,
                "pending_approval": pending_approval,
                "last_audit_head": last_audit_head,
            }
        )

    def queue_status_error(self, exc: Exception | None = None) -> None:
        self._status_queue.append(exc or ConnectionRefusedError("bus down"))

    def queue_enqueue_result(
        self, *, task_id: str, stream_path: str
    ) -> None:
        self._enqueue_queue.append(EnqueueResult(task_id=task_id, stream_path=stream_path))

    def queue_enqueue_error(self, exc: Exception | None = None) -> None:
        self._enqueue_queue.append(exc or RuntimeError("enqueue failed"))

    def queue_pause_ok(self) -> None:
        self._pause_results.append(True)

    def queue_pause_error(self, exc: Exception | None = None) -> None:
        self._pause_results.append(exc or RuntimeError("pause failed"))

    def queue_resume_ok(self) -> None:
        self._resume_results.append(True)

    def queue_resume_error(self, exc: Exception | None = None) -> None:
        self._resume_results.append(exc or RuntimeError("resume failed"))

    # Fake async D-Bus calls -----------------------------------------------

    async def call_GetQueueStatus(self) -> dict:  # noqa: N802
        if not self._status_queue:
            return {
                "state": "idle",
                "pending": 0,
                "in_progress": 0,
                "pending_approval": 0,
                "last_audit_head": "",
            }
        resp = self._status_queue.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def call_Enqueue(  # noqa: N802
        self,
        trigger_kind: str,
        text: str,
        priority: int,
        dedup_key: str,
        conversation_id: str = "",
        agent_id: str = "",
    ) -> EnqueueResult:
        self.enqueue_calls.append(
            {
                "trigger_kind": trigger_kind,
                "text": text,
                "priority": priority,
                "dedup_key": dedup_key,
                "conversation_id": conversation_id,
                "agent_id": agent_id,
            }
        )
        if not self._enqueue_queue:
            import uuid as _uuid

            tid = str(_uuid.uuid4())
            return EnqueueResult(task_id=tid, stream_path=f"/ws/tasks/{tid}")
        resp = self._enqueue_queue.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def call_Pause(self, reason: str) -> bool:  # noqa: N802
        self.pause_calls.append(reason)
        if not self._pause_results:
            return True
        resp = self._pause_results.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp  # type: ignore[return-value]

    async def call_Resume(self) -> bool:  # noqa: N802
        self.resume_calls += 1
        if not self._resume_results:
            return True
        resp = self._resume_results.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp  # type: ignore[return-value]

    # Roster multi-agente (stubs offline: sin daemon no hay roster que mostrar).
    async def call_ListAgents(self) -> str:  # noqa: N802
        return "[]"

    async def call_GetActiveAgent(self) -> str:  # noqa: N802
        return ""

    async def call_SetActiveAgent(self, agent_id: str) -> bool:  # noqa: N802
        return True

    async def call_CreateAgent(self, draft_json: str) -> str:  # noqa: N802
        return draft_json

    async def call_UpdateAgent(self, agent_id: str, draft_json: str) -> str:  # noqa: N802
        return draft_json

    async def call_DeleteAgent(self, agent_id: str) -> bool:  # noqa: N802
        return True

    # Gobernanza de skills (P0-1, stubs offline).
    async def call_ListSkills(self) -> str:  # noqa: N802
        return "[]"

    async def call_PromoteSkill(self, package_id: str) -> str:  # noqa: N802
        return "{}"

    async def call_DeprecateSkill(self, package_id: str) -> str:  # noqa: N802
        return "{}"

    async def call_SignComposioSkill(self, draft_json: str) -> str:  # noqa: N802
        return draft_json

    async def call_CreateSkillFromText(self, name: str, skill_md: str) -> str:  # noqa: N802
        """Stub: returns a minimal skill dict (no daemon in offline mode)."""
        import json as _json  # noqa: PLC0415
        return _json.dumps({"package_id": "", "skill_id": "", "skill_name": name, "version": 1})

    # Gobernanza de plataformas (feature 010, stubs offline).
    async def call_ListPlatformModels(self) -> str:  # noqa: N802
        return "[]"

    async def call_GetPlatformModelSummary(self, model_id: str) -> str:  # noqa: N802
        return "{}"

    async def call_ListAgentCapabilities(self, agent_id: str) -> str:  # noqa: N802
        return "[]"

    async def call_ListModelGaps(self, model_id: str) -> str:  # noqa: N802
        return "[]"

    async def call_StartPlatformTour(  # noqa: N802
        self, site_ref: str, origin: str, modality: str
    ) -> str:
        """Stub: returns a fake tour_id (no daemon in offline mode)."""
        import uuid as _uuid  # noqa: PLC0415
        return _uuid.uuid4().hex

    async def call_ClosePlatformTour(self, tour_id: str) -> str:  # noqa: N802
        import json as _json  # noqa: PLC0415
        return _json.dumps({"tour_id": tour_id, "model_compiled": False})

    async def call_ConfirmPlatformModel(  # noqa: N802
        self, model_id: str, corrections_json: str
    ) -> str:
        return "{}"

    async def call_EnablePlatformModel(self, model_id: str) -> bool:  # noqa: N802
        return True

    async def call_DisablePlatformModel(self, model_id: str) -> bool:  # noqa: N802
        return True

    async def call_DeprecatePlatformModel(self, model_id: str) -> bool:  # noqa: N802
        return True

    async def call_BindCapabilityToAgent(  # noqa: N802
        self,
        agent_id: str,
        capability_kind: str,
        capability_id: str,
        capability_version: str,
    ) -> str:
        return "{}"

    async def call_UnbindCapabilityFromAgent(  # noqa: N802
        self, agent_id: str, capability_kind: str, capability_id: str
    ) -> bool:
        return True

    async def call_SetAgentHouseRule(  # noqa: N802
        self, agent_id: str, model_id: str, rule_json: str
    ) -> bool:
        return True

    # Package Store stubs (offline: sin daemon no hay paquetes que listar).
    async def call_ListInstalledPackages(self, source: str) -> str:  # noqa: N802
        return "[]"

    async def call_SearchPackages(self, query: str, source: str) -> str:  # noqa: N802
        return "[]"

    async def call_InstallPackage(self, source: str, package_id: str) -> str:  # noqa: N802
        return "{}"

    async def call_UninstallPackage(self, source: str, package_id: str) -> str:  # noqa: N802
        return "{}"

    async def call_GetPkgOpStatus(self, op_id: str) -> str:  # noqa: N802
        return json.dumps({"op_id": op_id, "status": "unknown", "log_tail": "", "error_message": ""})


# ---------------------------------------------------------------------------
# FakeTaskStreamServer — in-memory fake for task stream tests
# ---------------------------------------------------------------------------


class FakeTaskStreamServer:
    """Returns pre-configured StreamFrames without a real socket."""

    def __init__(self) -> None:
        self._frames: list[StreamFrame | Exception] = []

    def queue_frame(
        self,
        *,
        kind: str,
        task_id: str,
        **payload_fields: Any,
    ) -> None:
        self._frames.append(
            StreamFrame(kind=kind, task_id=task_id, payload=payload_fields)
        )

    def queue_error(self, exc: Exception | None = None) -> None:
        self._frames.append(exc or ConnectionAbortedError("stream cut"))

    async def frames(self) -> AsyncIterator[StreamFrame]:
        for item in list(self._frames):
            if isinstance(item, Exception):
                raise item
            yield item


# ---------------------------------------------------------------------------
# TaskStreamClient — AF_UNIX WebSocket subscription (real implementation)
# ---------------------------------------------------------------------------

# WebSocket upgrade request/response helpers (minimal, no external dep)
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _build_ws_handshake(path: str, host: str = "localhost") -> bytes:
    """Build a minimal WebSocket upgrade request."""
    import base64
    import os

    key = base64.b64encode(os.urandom(16)).decode()
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        "",
        "",
    ]
    return "\r\n".join(lines).encode()


def _parse_ws_frame(data: bytes) -> tuple[bool, int, bytes, bytes]:
    """Parse a single WebSocket frame from `data`.

    Returns (fin, opcode, payload, remaining_bytes).
    Supports only unmasked server frames (client→server masking is not needed
    for reading from server).
    """
    if len(data) < 2:
        raise ValueError("incomplete ws frame header")
    fin = bool(data[0] & 0x80)
    opcode = data[0] & 0x0F
    masked = bool(data[1] & 0x80)
    payload_len = data[1] & 0x7F
    offset = 2
    if payload_len == 126:
        if len(data) < 4:
            raise ValueError("incomplete ws frame (126)")
        payload_len = int.from_bytes(data[2:4], "big")
        offset = 4
    elif payload_len == 127:
        if len(data) < 10:
            raise ValueError("incomplete ws frame (127)")
        payload_len = int.from_bytes(data[2:10], "big")
        offset = 10
    mask_key = b""
    if masked:
        mask_key = data[offset : offset + 4]
        offset += 4
    payload = data[offset : offset + payload_len]
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    remaining = data[offset + payload_len :]
    return fin, opcode, payload, remaining


class TaskStreamClient:
    """Subscribes to /ws/tasks/{task_id} over the AF_UNIX socket.

    The socket path is /run/hermes/tasks.sock (fixed, per spec).
    The task stream path (e.g. /ws/tasks/<uuid>) is returned by Enqueue.

    This client is used exclusively in non-test (VM) contexts.
    Tests inject FakeTaskStreamServer instead.

    Marked requires_vm: cannot be run in CI without a live daemon.
    """

    SOCKET_PATH = "/run/hermes/tasks.sock"

    def __init__(
        self,
        *,
        stream_path: str,
        socket_path: str = SOCKET_PATH,
        reconnect_on_disconnect: bool = True,
        max_reconnect_attempts: int = 3,
    ) -> None:
        self._stream_path = stream_path
        self._socket_path = socket_path
        self._reconnect = reconnect_on_disconnect
        self._max_reconnects = max_reconnect_attempts

    async def frames(self) -> AsyncIterator[StreamFrame]:
        """Yield StreamFrames from the daemon stream socket.

        On connection loss, re-attaches (re-GET) up to max_reconnect_attempts.
        Re-attach semantics per spec: daemon re-sends current status + done if
        already finished; the daemon never replays the full token history.
        """
        attempt = 0
        while True:
            try:
                async for frame in self._stream_once():
                    yield frame
                    if frame.kind == "done":
                        return
                return
            except (OSError, ConnectionError) as exc:
                attempt += 1
                if attempt > self._max_reconnects:
                    logger.error(
                        "task stream %s disconnected after %d reconnects: %s",
                        self._stream_path,
                        attempt,
                        exc,
                    )
                    raise
                logger.warning(
                    "task stream %s disconnected (attempt %d/%d): %s — reconnecting",
                    self._stream_path,
                    attempt,
                    self._max_reconnects,
                    exc,
                )
                await asyncio.sleep(0.3 * attempt)

    async def _stream_once(self) -> AsyncIterator[StreamFrame]:
        """Single connection attempt: connect, upgrade, read frames."""
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            await loop.sock_connect(sock, self._socket_path)
            # Send HTTP upgrade
            req = _build_ws_handshake(self._stream_path)
            await loop.sock_sendall(sock, req)
            # Read until end of HTTP headers (101 Switching Protocols)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = await loop.sock_recv(sock, 4096)
                if not chunk:
                    raise ConnectionError("socket closed during WS handshake")
                buf += chunk
            # Strip headers, remaining bytes are WS frame data
            _, _, after_headers = buf.partition(b"\r\n\r\n")
            buf = after_headers
            async for frame in self._read_frames(sock, buf):
                yield frame
        finally:
            try:
                sock.close()
            except OSError:
                pass

    async def _read_frames(
        self, sock: socket.socket, initial_buf: bytes
    ) -> AsyncIterator[StreamFrame]:
        loop = asyncio.get_event_loop()
        buf = initial_buf
        while True:
            if len(buf) < 2:
                chunk = await loop.sock_recv(sock, 65536)
                if not chunk:
                    return
                buf += chunk
                continue
            try:
                fin, opcode, payload, buf = _parse_ws_frame(buf)
            except ValueError:
                # Need more data
                chunk = await loop.sock_recv(sock, 65536)
                if not chunk:
                    return
                buf += chunk
                continue

            if opcode == 8:  # close
                return
            if opcode == 9:  # ping → send pong
                pong = bytes([0x8A, 0x00])
                await loop.sock_sendall(sock, pong)
                continue
            if opcode in (1, 2) and payload:  # text or binary
                try:
                    frame = StreamFrame.from_json(payload.decode("utf-8", errors="replace"))
                    yield frame
                    if frame.kind == "done":
                        return
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.warning("malformed stream frame: %s", exc)


# ---------------------------------------------------------------------------
# RealDbusInterface — thin adapter over a live dbus-fast proxy interface
# ---------------------------------------------------------------------------


class RealDbusInterface:
    """Adapts a dbus-fast proxy interface (lowercase call_* methods) to the
    same async call_Enqueue / call_GetQueueStatus / call_Pause / call_Resume
    surface that DbusRuntimeClient expects from FakeDbusInterface.

    This is the only object that knows about dbus-fast naming conventions.
    DbusRuntimeClient never imports dbus-fast directly; it always calls through
    this surface — keeping the real bus entirely out of unit tests.

    The proxy is obtained once during construction (connect → introspect →
    get_proxy_object → get_interface) and reused for all method calls.  Bus
    lifecycle (connect/disconnect on reconnect) is managed by the caller
    (_wire_dbus_client in window.py) which replaces this object on reconnect.
    """

    def __init__(self, *, proxy_interface: Any) -> None:
        # proxy_interface: the object returned by dbus-fast get_interface().
        # dbus-fast generates call_<lowercase_member> methods on it.
        self._iface = proxy_interface

    async def call_GetQueueStatus(self) -> dict:  # noqa: N802
        raw = await self._iface.call_get_queue_status()
        # dbus-fast returns a{sv} as a dict[str, Variant]; unwrap values.
        return {k: (v.value if hasattr(v, "value") else v) for k, v in raw.items()}

    async def call_Enqueue(  # noqa: N802
        self,
        trigger_kind: str,
        text: str,
        priority: int,
        dedup_key: str,
        conversation_id: str = "",
        agent_id: str = "",
    ) -> EnqueueResult:
        # Daemon Enqueue signature: (s, s, i, s, s, s, s) -> (s, s)
        # The GTK shell runs IN the hermes-user session (uid ∈ authorized_uids),
        # so it is a DIRECT caller — the daemon ignores the (empty) operator_token
        # on the direct path. Proxy callers (shell-server) pass a signed token.
        # agent_id: per-conversation bound agent ("" = resolve to CEO server-side).
        task_id_str, stream_path = await self._iface.call_enqueue(
            trigger_kind,
            text,
            priority,
            dedup_key,
            conversation_id,
            "",
            agent_id,
        )
        return EnqueueResult(task_id=task_id_str, stream_path=stream_path)

    async def call_Pause(self, reason: str) -> bool:  # noqa: N802
        return await self._iface.call_pause(reason)

    async def call_Resume(self) -> bool:  # noqa: N802
        return await self._iface.call_resume()

    # --- Gobernanza del roster multi-agente (JSON sobre D-Bus) ---
    async def call_ListAgents(self) -> str:  # noqa: N802
        return await self._iface.call_list_agents()

    async def call_GetActiveAgent(self) -> str:  # noqa: N802
        return await self._iface.call_get_active_agent()

    async def call_SetActiveAgent(self, agent_id: str) -> bool:  # noqa: N802
        return await self._iface.call_set_active_agent(agent_id)

    async def call_CreateAgent(self, draft_json: str) -> str:  # noqa: N802
        return await self._iface.call_create_agent(draft_json)

    async def call_UpdateAgent(self, agent_id: str, draft_json: str) -> str:  # noqa: N802
        return await self._iface.call_update_agent(agent_id, draft_json)

    async def call_DeleteAgent(self, agent_id: str) -> bool:  # noqa: N802
        return await self._iface.call_delete_agent(agent_id)

    # --- Gobernanza de skills (JSON sobre D-Bus, P0-1) ---
    async def call_ListSkills(self) -> str:  # noqa: N802
        return await self._iface.call_list_skills()

    async def call_PromoteSkill(self, package_id: str) -> str:  # noqa: N802
        return await self._iface.call_promote_skill(package_id)

    async def call_DeprecateSkill(self, package_id: str) -> str:  # noqa: N802
        return await self._iface.call_deprecate_skill(package_id)

    async def call_SignComposioSkill(self, draft_json: str) -> str:  # noqa: N802
        return await self._iface.call_sign_composio_skill(draft_json)

    async def call_CreateSkillFromText(self, name: str, skill_md: str) -> str:  # noqa: N802
        return await self._iface.call_create_skill_from_text(name, skill_md)

    # --- Gobernanza de plataformas (feature 010, JSON sobre D-Bus) ---

    async def call_ListPlatformModels(self) -> str:  # noqa: N802
        return await self._iface.call_list_platform_models()

    async def call_GetPlatformModelSummary(self, model_id: str) -> str:  # noqa: N802
        return await self._iface.call_get_platform_model_summary(model_id)

    async def call_ListAgentCapabilities(self, agent_id: str) -> str:  # noqa: N802
        return await self._iface.call_list_agent_capabilities(agent_id)

    async def call_ListModelGaps(self, model_id: str) -> str:  # noqa: N802
        return await self._iface.call_list_model_gaps(model_id)

    async def call_StartPlatformTour(  # noqa: N802
        self, site_ref: str, origin: str, modality: str
    ) -> str:
        return await self._iface.call_start_platform_tour(site_ref, origin, modality)

    async def call_ClosePlatformTour(self, tour_id: str) -> str:  # noqa: N802
        return await self._iface.call_close_platform_tour(tour_id)

    async def call_ConfirmPlatformModel(  # noqa: N802
        self, model_id: str, corrections_json: str
    ) -> str:
        return await self._iface.call_confirm_platform_model(model_id, corrections_json)

    async def call_EnablePlatformModel(self, model_id: str) -> bool:  # noqa: N802
        return await self._iface.call_enable_platform_model(model_id)

    async def call_DisablePlatformModel(self, model_id: str) -> bool:  # noqa: N802
        return await self._iface.call_disable_platform_model(model_id)

    async def call_DeprecatePlatformModel(self, model_id: str) -> bool:  # noqa: N802
        return await self._iface.call_deprecate_platform_model(model_id)

    async def call_BindCapabilityToAgent(  # noqa: N802
        self,
        agent_id: str,
        capability_kind: str,
        capability_id: str,
        capability_version: str,
    ) -> str:
        return await self._iface.call_bind_capability_to_agent(
            agent_id, capability_kind, capability_id, capability_version
        )

    async def call_UnbindCapabilityFromAgent(  # noqa: N802
        self, agent_id: str, capability_kind: str, capability_id: str
    ) -> bool:
        return await self._iface.call_unbind_capability_from_agent(
            agent_id, capability_kind, capability_id
        )

    async def call_SetAgentHouseRule(  # noqa: N802
        self, agent_id: str, model_id: str, rule_json: str
    ) -> bool:
        return await self._iface.call_set_agent_house_rule(agent_id, model_id, rule_json)

    # --- Package Store (Flatpak + RPM, JSON sobre D-Bus) ---
    async def call_ListInstalledPackages(self, source: str) -> str:  # noqa: N802
        return await self._iface.call_list_installed_packages(source)

    async def call_SearchPackages(self, query: str, source: str) -> str:  # noqa: N802
        return await self._iface.call_search_packages(query, source)

    async def call_InstallPackage(self, source: str, package_id: str) -> str:  # noqa: N802
        return await self._iface.call_install_package(source, package_id)

    async def call_UninstallPackage(self, source: str, package_id: str) -> str:  # noqa: N802
        return await self._iface.call_uninstall_package(source, package_id)

    async def call_GetPkgOpStatus(self, op_id: str) -> str:  # noqa: N802
        return await self._iface.call_get_pkg_op_status(op_id)


async def build_real_dbus_interface() -> "RealDbusInterface":
    """Connect to the system bus and return a RealDbusInterface.

    This coroutine must run on the shell's async thread (NOT the GTK main
    thread).  It blocks until the bus connection is established and introspection
    completes — typically < 50 ms on a healthy bus.

    Raises:
        ImportError:  dbus-fast not installed (not a production OS image).
        Exception:    daemon not present on the bus (name not found, timeout).
    """
    from dbus_fast.aio import MessageBus  # noqa: PLC0415
    from dbus_fast import BusType  # noqa: PLC0415

    _WELL_KNOWN_NAME = "org.hermes.Runtime"
    _OBJECT_PATH = "/org/hermes/Runtime"
    _INTERFACE_NAME = "org.hermes.Runtime1"

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    # Introspect resolves the XML contract and builds the proxy object with
    # call_<method> stubs.  Without introspection dbus-fast cannot generate
    # the typed call_enqueue helper.
    introspection = await bus.introspect(_WELL_KNOWN_NAME, _OBJECT_PATH)
    proxy = bus.get_proxy_object(_WELL_KNOWN_NAME, _OBJECT_PATH, introspection)
    iface = proxy.get_interface(_INTERFACE_NAME)
    # Store bus on the interface so the caller can call bus.disconnect() later.
    iface._bus = bus  # type: ignore[attr-defined]
    return RealDbusInterface(proxy_interface=iface)


# ---------------------------------------------------------------------------
# DbusRuntimeClient — implements AgentRuntimePort
# ---------------------------------------------------------------------------


class DbusRuntimeClient:
    """Shell-side D-Bus proxy for org.hermes.Runtime1.

    Accepts any object with the async call_* interface (FakeDbusInterface in
    tests, the real dbus-fast proxy in production/VM).

    Implements:
      - get_status() -> dict  (consumed by RuntimeBackendHealthMonitor)
      - enqueue(kind, text, conversation_id) -> (task_id, stream_path)
      - request_pause(reason) / request_resume()

    Note on legacy send_message:
      The old AgentRuntimePort.send_message() was a WebSocket passthrough.
      This client replaces it with enqueue() + TaskStreamClient subscription.
      The old method is retained as a thin shim that calls enqueue() so
      existing code that uses send_message still compiles; callers should
      migrate to enqueue() + subscribe_task_stream().
    """

    def __init__(
        self,
        *,
        dbus_interface: Any,  # FakeDbusInterface | real dbus-fast proxy
    ) -> None:
        self._iface = dbus_interface

    # ------------------------------------------------------------------
    # AgentRuntimePort — get_status
    # ------------------------------------------------------------------

    async def get_status(self) -> dict:
        """Delegates to GetQueueStatus on the D-Bus interface.

        Returns a dict with at least {"status": "ok"} or {"status": "degraded"}.
        The monitor polls this to drive RuntimeLinkState transitions.
        """
        raw = await self._iface.call_GetQueueStatus()
        state = raw.get("state", "idle")
        # Translate daemon states to the health monitor vocabulary:
        # degraded ← agent paused or no-model (has_model=false)
        if state in ("paused", "no_model"):
            return {"status": "degraded", "raw_state": state, **raw}
        return {"status": "ok", "raw_state": state, **raw}

    # ------------------------------------------------------------------
    # Enqueue (replaces send_message passthrough)
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        *,
        kind: str,
        text: str,
        conversation_id: str | None = None,
        priority: int = 0,
        dedup_key: str = "",
    ) -> tuple[str, str]:
        """Call Enqueue on org.hermes.Runtime1.

        Returns (task_id, stream_path). The caller then passes stream_path
        to subscribe_task_stream() to receive the response frames.

        conversation_id is passed as the dedicated 5th D-Bus argument ("s").
        The daemon Enqueue signature is:
            (trigger_kind: s, text: s, priority: i, dedup_key: s,
             conversation_id: s) -> (task_id: s, stream_path: s)
        An empty string means "no conversation_id" (daemon treats "" as None).
        For chat_message triggers the daemon enforces invariant I5: a non-empty
        conversation_id is required or the work item is silently dropped by
        INSERT OR IGNORE on the CHECK constraint.  The caller (window.py) always
        generates a UUID before calling enqueue() for chat_message, so this
        never reaches the daemon empty for that trigger kind.
        """
        result = await self._iface.call_Enqueue(
            kind,
            text,
            priority,
            dedup_key,
            conversation_id or "",
        )
        return result.task_id, result.stream_path

    # ------------------------------------------------------------------
    # Pause / Resume
    # ------------------------------------------------------------------

    async def request_pause(self, *, reason: str) -> None:
        await self._iface.call_Pause(reason)

    async def request_resume(self) -> None:
        await self._iface.call_Resume()

    # ------------------------------------------------------------------
    # Gobernanza del roster multi-agente (Principio 0: estado del daemon vía D-Bus)
    # ------------------------------------------------------------------
    async def list_agents(self) -> list[dict]:
        raw = await self._iface.call_ListAgents()
        return json.loads(raw) if raw else []

    async def get_active_agent(self) -> str:
        return await self._iface.call_GetActiveAgent()

    async def set_active_agent(self, agent_id: str) -> None:
        await self._iface.call_SetActiveAgent(agent_id)

    async def create_agent(self, draft: dict) -> dict:
        raw = await self._iface.call_CreateAgent(json.dumps(draft))
        return json.loads(raw)

    async def update_agent(self, agent_id: str, draft: dict) -> dict:
        raw = await self._iface.call_UpdateAgent(agent_id, json.dumps(draft))
        return json.loads(raw)

    async def delete_agent(self, agent_id: str) -> None:
        await self._iface.call_DeleteAgent(agent_id)

    # ------------------------------------------------------------------
    # Gobernanza de skills (Principio 0: estado del daemon vía D-Bus / P0-1)
    # ------------------------------------------------------------------

    async def list_skills(self) -> list[dict]:
        raw = await self._iface.call_ListSkills()
        return json.loads(raw) if raw else []

    async def promote_skill(self, package_id: str) -> dict:
        raw = await self._iface.call_PromoteSkill(package_id)
        return json.loads(raw) if raw else {}

    async def deprecate_skill(self, package_id: str) -> dict:
        raw = await self._iface.call_DeprecateSkill(package_id)
        return json.loads(raw) if raw else {}

    async def sign_composio_skill(self, draft: dict) -> dict:
        raw = await self._iface.call_SignComposioSkill(json.dumps(draft))
        return json.loads(raw) if raw else {}

    # ------------------------------------------------------------------
    # Gobernanza de plataformas (feature 010, Principio 0)
    # ------------------------------------------------------------------

    async def list_platform_models(self) -> list[dict]:
        raw = await self._iface.call_ListPlatformModels()
        return json.loads(raw) if raw else []

    async def get_platform_model_summary(self, model_id: str) -> dict:
        raw = await self._iface.call_GetPlatformModelSummary(model_id)
        return json.loads(raw) if raw else {}

    async def list_agent_capabilities(self, agent_id: str) -> list[dict]:
        raw = await self._iface.call_ListAgentCapabilities(agent_id)
        return json.loads(raw) if raw else []

    async def list_model_gaps(self, model_id: str) -> list[dict]:
        raw = await self._iface.call_ListModelGaps(model_id)
        return json.loads(raw) if raw else []

    async def start_platform_tour(
        self, site_ref: str, origin: str = "guided", modality: str = "text_only"
    ) -> str:
        return await self._iface.call_StartPlatformTour(site_ref, origin, modality)

    async def close_platform_tour(self, tour_id: str) -> dict:
        raw = await self._iface.call_ClosePlatformTour(tour_id)
        return json.loads(raw) if raw else {}

    async def confirm_platform_model(
        self, model_id: str, corrections: list | None = None
    ) -> dict:
        corrections_json = json.dumps(corrections or [])
        raw = await self._iface.call_ConfirmPlatformModel(model_id, corrections_json)
        return json.loads(raw) if raw else {}

    async def enable_platform_model(self, model_id: str) -> bool:
        return await self._iface.call_EnablePlatformModel(model_id)

    async def disable_platform_model(self, model_id: str) -> bool:
        return await self._iface.call_DisablePlatformModel(model_id)

    async def deprecate_platform_model(self, model_id: str) -> bool:
        return await self._iface.call_DeprecatePlatformModel(model_id)

    async def bind_capability_to_agent(
        self,
        agent_id: str,
        capability_kind: str,
        capability_id: str,
        capability_version: str,
    ) -> dict:
        raw = await self._iface.call_BindCapabilityToAgent(
            agent_id, capability_kind, capability_id, capability_version
        )
        return json.loads(raw) if raw else {}

    async def unbind_capability_from_agent(
        self, agent_id: str, capability_kind: str, capability_id: str
    ) -> bool:
        return await self._iface.call_UnbindCapabilityFromAgent(
            agent_id, capability_kind, capability_id
        )

    async def set_agent_house_rule(
        self, agent_id: str, model_id: str, rule: dict
    ) -> bool:
        return await self._iface.call_SetAgentHouseRule(
            agent_id, model_id, json.dumps(rule)
        )

    # ------------------------------------------------------------------
    # Package Store (Flatpak + RPM, Principio 0: estado vía D-Bus)
    # ------------------------------------------------------------------

    async def list_installed_packages(self, source: str) -> list[dict]:
        raw = await self._iface.call_ListInstalledPackages(source)
        return json.loads(raw) if raw else []

    async def search_packages(self, query: str, source: str = "all") -> list[dict]:
        raw = await self._iface.call_SearchPackages(query, source)
        return json.loads(raw) if raw else []

    async def install_package(self, source: str, package_id: str) -> dict:
        raw = await self._iface.call_InstallPackage(source, package_id)
        return json.loads(raw) if raw else {}

    async def uninstall_package(self, source: str, package_id: str) -> dict:
        raw = await self._iface.call_UninstallPackage(source, package_id)
        return json.loads(raw) if raw else {}

    async def get_pkg_op_status(self, op_id: str) -> dict:
        raw = await self._iface.call_GetPkgOpStatus(op_id)
        return json.loads(raw) if raw else {}

    # ------------------------------------------------------------------
    # Legacy shim: send_message (AgentRuntimePort compat)
    # ------------------------------------------------------------------

    async def send_message(self, *, text: str):  # noqa: ANN201
        """Shim for AgentRuntimePort.send_message — enqueues a chat_message.

        Returns an AsyncIterator[AgentResponseChunk] to satisfy the old port
        signature. Callers should prefer enqueue() + subscribe_task_stream().
        """
        from hermes.shell.application.ports import AgentResponseChunk

        task_id, stream_path = await self.enqueue(kind="chat_message", text=text)
        client = TaskStreamClient(stream_path=stream_path)

        async def _iter():
            async for frame in client.frames():
                if frame.kind == "delta":
                    yield AgentResponseChunk(
                        delta=frame.payload.get("delta", ""),
                        is_final=False,
                    )
                elif frame.kind == "done":
                    yield AgentResponseChunk(delta="", is_final=True)

        return _iter()

    # ------------------------------------------------------------------
    # Stream subscription factory
    # ------------------------------------------------------------------

    def subscribe_task_stream(
        self,
        *,
        stream_path: str,
        socket_path: str = TaskStreamClient.SOCKET_PATH,
    ) -> TaskStreamClient:
        """Return a TaskStreamClient bound to the given stream_path."""
        return TaskStreamClient(stream_path=stream_path, socket_path=socket_path)
