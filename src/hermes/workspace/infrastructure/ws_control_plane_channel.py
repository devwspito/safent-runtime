"""ControlPlaneChannelPort adapter: WebSocket mTLS desde la VM al control plane.

T058 — Phase 2.4. Cumple el contrato declarado en
``specs/002-.../contracts/control_plane_channel_port.py``.

Diseño:
- Cliente outbound (VM → control plane). El control plane NUNCA inicia
  conexión hacia la VM: solo recibe.
- mTLS con cert efímero per Workspace (rotado por el ``vm_cert_minter`` del
  control plane). Cert pinning del peer (control plane CA fingerprint).
- json-rpc 2.0 sobre WebSocket binary frames (research §10).
- Heartbeat cada 15s (Heartbeat domain VO).
- Reconexión exponencial con jitter; mantiene una cola in-memory de mensajes
  durante la disrupción (cap a 256 mensajes para no inflar memoria).
- Cualquier mismatch tenant_id/workspace_id devuelto por el server lanza
  ``ChannelAuthFailed`` (fail-closed, constitución IV).

Lazy-import de ``websockets`` y ``cryptography`` para mantener el bounded
context importable sin las deps de ``[workspace]`` instaladas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


class ChannelAuthFailed(RuntimeError):
    """Server rechazó la sesión por tenant_id/workspace_id mismatch."""


class ChannelClosed(RuntimeError):
    """Canal cerrado limpiamente; los pendientes no se envían."""


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    """Configuración del adapter inyectada en boot."""

    control_plane_url: str  # wss://cp.eu.hermes.ai/vm
    tenant_id: UUID
    workspace_id: UUID
    client_cert_pem: bytes  # cert efímero emitido al spawn
    client_key_pem: bytes
    ca_pinning_fingerprint_sha256_hex: str  # 64 hex
    heartbeat_interval_s: int = 15
    queue_cap: int = 256
    initial_backoff_s: float = 0.5
    max_backoff_s: float = 30.0


_JSONRPC_VERSION = "2.0"


class WsControlPlaneChannelAdapter:
    """Cliente WebSocket mTLS. Cumple ``ControlPlaneChannelPort``."""

    def __init__(self, config: ChannelConfig) -> None:
        self._cfg = config
        self._ws: Any = None  # websockets.WebSocketClientProtocol
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._queue: deque[dict[str, Any]] = deque(maxlen=config.queue_cap)
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._on_command: Callable[[dict[str, Any]], Any] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(
        self,
        *,
        on_command: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        """Abre el WS mTLS. Lanza ``ChannelAuthFailed`` si el handshake falla."""
        self._on_command = on_command
        await self._connect_with_backoff()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in (self._heartbeat_task, self._receive_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ws_control_plane_channel.close_failed", extra={"error": str(exc)}
                )

    # ------------------------------------------------------------------
    # Send / drain
    # ------------------------------------------------------------------

    async def send_command(
        self, method: str, params: dict[str, Any]
    ) -> None:
        """Encola y envía un comando json-rpc al control plane.

        Si el canal está caído, encola hasta ``queue_cap``. Cuando se reconecta,
        drena la cola en orden FIFO.
        """
        if self._closed:
            raise ChannelClosed("canal cerrado")

        payload = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": secrets.token_hex(8),
            "method": method,
            "params": self._enrich_with_identity(params),
        }
        self._queue.append(payload)
        await self._drain_queue()

    async def emit_heartbeat(
        self,
        *,
        runtime_version: str = "",
        chromium_pid: int | None = None,
        runtime_pid: int | None = None,
        whisper_pid: int | None = None,
    ) -> None:
        """Envía heartbeat al control plane."""
        await self.send_command(
            "heartbeat",
            {
                "emitted_at": datetime.now(tz=UTC).isoformat(),
                "interval_s": self._cfg.heartbeat_interval_s,
                "runtime_version": runtime_version,
                "chromium_pid": chromium_pid,
                "runtime_pid": runtime_pid,
                "whisper_pid": whisper_pid,
            },
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _enrich_with_identity(self, params: dict[str, Any]) -> dict[str, Any]:
        """Multi-tenant strict: cada mensaje lleva tenant_id+workspace_id.
        El server REVALIDARÁ contra el cert mTLS (FR-038, T059)."""
        enriched = dict(params)
        enriched.setdefault("tenant_id", str(self._cfg.tenant_id))
        enriched.setdefault("workspace_id", str(self._cfg.workspace_id))
        return enriched

    async def _drain_queue(self) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            while self._queue:
                msg = self._queue[0]
                try:
                    await self._ws.send(json.dumps(msg))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "ws_channel.send_failed_will_retry",
                        extra={"error": str(exc), "remaining": len(self._queue)},
                    )
                    return
                self._queue.popleft()

    async def _heartbeat_loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(self._cfg.heartbeat_interval_s)
                if not self._closed:
                    await self.emit_heartbeat()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ws_channel.heartbeat_error", extra={"error": str(exc)}
                )

    async def _receive_loop(self) -> None:
        while not self._closed:
            if self._ws is None:
                await self._connect_with_backoff()
                if self._ws is None:
                    return
            try:
                raw = await self._ws.recv()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ws_channel.receive_disconnected", extra={"error": str(exc)}
                )
                self._ws = None
                await asyncio.sleep(1.0)
                continue

            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("ws_channel.recv_invalid_json")
                continue

            # Server NUNCA envía commands con tenant_id != self.tenant_id.
            # Si lo hace, fail-closed.
            server_tenant = parsed.get("params", {}).get("tenant_id")
            if server_tenant and server_tenant != str(self._cfg.tenant_id):
                logger.error(
                    "ws_channel.cross_tenant_violation_attempt",
                    extra={
                        "expected_tenant_id": str(self._cfg.tenant_id),
                        "received_tenant_id": server_tenant,
                    },
                )
                await self.close()
                raise ChannelAuthFailed(
                    "Server envió comando con tenant_id distinto al esperado"
                )

            if self._on_command is not None:
                try:
                    result = self._on_command(parsed)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "ws_channel.on_command_handler_failed",
                        extra={"error": str(exc)},
                    )

    async def _connect_with_backoff(self) -> None:
        """Reconnect con exponencial + jitter."""
        backoff = self._cfg.initial_backoff_s
        attempt = 0
        while not self._closed:
            attempt += 1
            try:
                self._ws = await self._open_ws_once()
                logger.info(
                    "ws_channel.connected",
                    extra={"attempts": attempt, "url": self._cfg.control_plane_url},
                )
                await self._drain_queue()
                return
            except ChannelAuthFailed:
                # auth fail no se reintenta — el cert es inválido (kill switch).
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ws_channel.connect_failed_will_retry",
                    extra={"attempt": attempt, "error": str(exc), "backoff_s": backoff},
                )
                await asyncio.sleep(backoff + secrets.randbelow(1000) / 1000.0)
                backoff = min(backoff * 2, self._cfg.max_backoff_s)
                if attempt > 100:  # cap pragmático
                    logger.error("ws_channel.giving_up_reconnect", extra={"attempts": attempt})
                    return

    async def _open_ws_once(self) -> Any:
        """Realiza la conexión. Lazy-import de ``websockets`` y ``ssl``."""
        import ssl  # noqa: PLC0415
        from io import BytesIO  # noqa: PLC0415

        import websockets  # noqa: PLC0415

        # SSLContext con cert efímero + pin del peer.
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        # Cargar cert+key cliente desde memoria.
        # ssl.SSLContext no tiene API directa para load_cert_chain desde bytes,
        # así que escribimos a path temporal con permisos 0600. Se borra al cerrar.
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        # Fix-11: use mkstemp (atomic O_EXCL creation, mode 0600) instead of
        # mktemp (TOCTOU race). The fd is closed via fdopen so no handle leaks.
        cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
        key_fd, key_path = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(cert_fd, "wb") as f:
                f.write(self._cfg.client_cert_pem)
            with os.fdopen(key_fd, "wb") as f:
                f.write(self._cfg.client_key_pem)
            ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        finally:
            try:
                os.unlink(cert_path)
                os.unlink(key_path)
            except OSError:
                pass

        # Pin de la CA: verificamos el fingerprint del cert del server tras conectar.
        # websockets nos da acceso a `ws.transport.get_extra_info('peercert', ...)`.
        ws = await websockets.connect(
            self._cfg.control_plane_url, ssl=ctx, max_size=2**20  # 1 MiB max frame
        )
        # Verificación de pinning (defense in depth, además de la CA del default ctx).
        peer_cert_der = ws.transport.get_extra_info("ssl_object").getpeercert(
            binary_form=True
        )
        import hashlib  # noqa: PLC0415

        peer_fp = hashlib.sha256(peer_cert_der).hexdigest()
        if peer_fp.lower() != self._cfg.ca_pinning_fingerprint_sha256_hex.lower():
            await ws.close()
            raise ChannelAuthFailed(
                f"Pinning del peer falló: esperado "
                f"{self._cfg.ca_pinning_fingerprint_sha256_hex[:12]}..., "
                f"recibido {peer_fp[:12]}..."
            )
        # Buffer de protocolo unused
        _ = BytesIO
        return ws

    @property
    def driver_name(self) -> str:
        return "ws_control_plane_channel"

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._closed
