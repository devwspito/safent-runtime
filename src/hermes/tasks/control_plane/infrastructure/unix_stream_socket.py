"""T044 — UnixStreamSocket: servidor WS sobre AF_UNIX para el stream de tareas.

Infraestructura. Adapta el StreamBroker (application layer) al transporte
WebSocket sobre socket Unix (AF_UNIX /run/hermes/tasks.sock).

Seguridad (NFR-007, CTRL-P1-8/9):
  - Permisos 0660 root:hermes en el socket file.
  - SO_PEERCRED en accept(): solo el UID autorizado puede leer chunks.
    PII viaja por canal local — NUNCA por el provider de inferencia.
  - Fail-closed: conexión sin UID autorizado → HTTP 403, sin stack trace.

Protocolo (task_stream_socket_v1.md):
  - Ruta lógica: GET /ws/tasks/{task_id}
  - JSONL frame por mensaje WS (TaskStreamFrame).
  - Primer frame SIEMPRE es kind=STATUS con protocol_version.
  - Daemon-owned: el socket es único writer; el shell-server solo lee.

Back-pressure:
  El StreamBroker descarta deltas para clientes lentos (best-effort).
  El socket no bloquea el ciclo del daemon.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import struct
from collections.abc import Callable
from uuid import UUID

from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve as ws_serve

from hermes.tasks.control_plane.application.stream_broker import StreamBroker

logger = logging.getLogger("hermes.tasks.unix_stream_socket")

# Ruta por defecto del socket (tmpfiles la gestiona en producción)
_DEFAULT_SOCK_PATH = "/run/hermes/tasks.sock"

# Regex para extraer task_id de la ruta /ws/tasks/{task_id}
_PATH_RE = re.compile(r"^/ws/tasks/([0-9a-f-]{36})$", re.IGNORECASE)

# Tamaño del struct SO_PEERCRED (pid, uid, gid — 3 unsigned int)
_PEERCRED_SIZE = struct.calcsize("3I")


def _extract_peer_uid(ws: ServerConnection) -> int | None:
    """Extrae el UID del proceso par via SO_PEERCRED.

    Retorna None si el socket no es AF_UNIX o SO_PEERCRED no está disponible.
    """
    raw_sock: socket.socket | None = ws.transport.get_extra_info("socket")
    if raw_sock is None:
        return None
    try:
        cred_bytes = raw_sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED_SIZE
        )
        _, uid, _ = struct.unpack("3I", cred_bytes)
        return uid
    except OSError:
        return None


def _extract_task_id(path: str) -> UUID | None:
    """Extrae el task_id de la ruta /ws/tasks/{task_id}. None si inválida."""
    m = _PATH_RE.match(path)
    if m is None:
        return None
    try:
        return UUID(m.group(1))
    except ValueError:
        return None


class UnixStreamSocketServer:
    """Servidor WS sobre AF_UNIX que adapta StreamBroker al transporte de red.

    Uso:
        server = UnixStreamSocketServer(
            broker=broker,
            authorized_uid=os.getuid(),
            sock_path="/run/hermes/tasks.sock",
        )
        await server.serve_forever()   # bloqueante hasta shutdown
        server.close()                 # señaliza parada

    El servidor NO cambia permisos de grupo (debe existir el dir /run/hermes
    con permisos correctos, gestionado por tmpfiles.d en producción).
    En tests el sock_path es un tmp path sin restricciones de permisos.
    """

    def __init__(
        self,
        *,
        broker: StreamBroker,
        authorized_uid: int,
        sock_path: str = _DEFAULT_SOCK_PATH,
    ) -> None:
        self._broker = broker
        # Authorize the operator AND the daemon's own service uid (os.getuid()).
        # The shell-server (web UI backend) runs as the same service user as the
        # daemon (hermes) and must read task streams to relay them to the browser;
        # both are trusted first-party. The operator (graphical/TUI client) is the
        # other authorized reader. Any other uid is rejected via SO_PEERCRED.
        self._authorized_uid = authorized_uid
        self._authorized_uids = frozenset({authorized_uid, os.getuid()})
        self._sock_path = sock_path
        self._server: object | None = None
        self._shutdown = asyncio.Event()

    async def serve_forever(self) -> None:
        """Arranca el servidor y bloquea hasta close()."""
        process_request = self._make_auth_guard()

        async with ws_serve(
            self._handle_connection,
            unix=True,
            path=self._sock_path,
            process_request=process_request,
            ping_interval=None,
            ping_timeout=None,
        ) as server:
            self._server = server
            # ws_serve crea el socket con la umask del proceso (→ 0755, sin write
            # de grupo). El operador gráfico (hermes-user, miembro del grupo
            # hermes) necesita permiso de ESCRITURA para connect() a un AF_UNIX.
            # Lo forzamos a 0o660 → el grupo hermes puede conectar. La defensa
            # real no es el DAC sino SO_PEERCRED (check_auth) más abajo.
            try:
                os.chmod(self._sock_path, 0o660)
            except OSError as exc:
                logger.warning(
                    "hermes.tasks.unix_stream_socket.chmod_failed",
                    extra={"path": self._sock_path, "error": str(exc)},
                )
            logger.info(
                "hermes.tasks.unix_stream_socket.started",
                extra={"path": self._sock_path, "authorized_uid": self._authorized_uid},
            )
            await self._shutdown.wait()

        logger.info("hermes.tasks.unix_stream_socket.stopped")

    def close(self) -> None:
        """Señaliza parada limpia."""
        self._shutdown.set()

    # ------------------------------------------------------------------
    # Private: auth guard + connection handler
    # ------------------------------------------------------------------

    def _make_auth_guard(self) -> Callable:
        """Fabrica el process_request que verifica SO_PEERCRED en accept()."""
        authorized_uids = self._authorized_uids

        def check_auth(ws: ServerConnection, request: object) -> object | None:  # noqa: ARG001
            peer_uid = _extract_peer_uid(ws)
            if peer_uid not in authorized_uids:
                logger.warning(
                    "hermes.tasks.unix_stream_socket.unauthorized",
                    extra={"peer_uid": peer_uid, "authorized_uids": sorted(authorized_uids)},
                )
                return ws.respond(403, "Forbidden: unauthorized UID")
            return None

        return check_auth

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """Maneja una conexión WS autenticada.

        1. Extrae task_id de la ruta.
        2. Envía frame STATUS inicial (protocol_version incluido — v1 contrato).
        3. Fan-out desde el broker (re-attach si ya terminó).
        4. Cierra limpiamente.
        """
        path = ws.request.path if ws.request else "/"
        task_id = _extract_task_id(path)

        if task_id is None:
            logger.warning(
                "hermes.tasks.unix_stream_socket.invalid_path",
                extra={"path": path},
            )
            await ws.close(1008, "Invalid path")
            return

        logger.debug(
            "hermes.tasks.unix_stream_socket.client_connected",
            extra={"task_id": str(task_id), "path": path},
        )

        try:
            await self._stream_task(ws, task_id)
        except Exception:
            logger.exception(
                "hermes.tasks.unix_stream_socket.handler_error",
                extra={"task_id": str(task_id)},
            )

    async def _stream_task(self, ws: ServerConnection, task_id: UUID) -> None:
        """Itera los frames del broker y los envía al cliente WS."""
        async for frame in self._broker.subscribe(task_id=task_id):
            try:
                await ws.send(frame.to_jsonl())
            except Exception:
                logger.debug(
                    "hermes.tasks.unix_stream_socket.send_error",
                    extra={"task_id": str(task_id)},
                )
                break
