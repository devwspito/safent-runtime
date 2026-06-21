"""T036 — Integration: socket Unix WS + StreamBroker (US2 Fase B2+B3).

Verifica los contratos del socket de stream de tareas (task_stream_socket_v1.md):
  - SO_PEERCRED rechaza UID no autorizado → HTTP 403.
  - Suscripción a task_id segregada: dos tareas no mezclan chunks.
  - Re-attach reenvía estado actual (status + done), NO histórico de tokens.
  - Back-pressure: deltas se descartan para cliente lento sin bloquear al broker.

Cada test arranca un servidor real sobre socket Unix en tmp_path.
El fake daemon publica frames directamente al StreamBroker.
Sin D-Bus, sin SQLite — solo la capa de transport + broker.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import websockets
from websockets.asyncio.client import unix_connect

from hermes.tasks.control_plane.application.stream_broker import StreamBroker
from hermes.tasks.control_plane.domain.task_stream_frame import (
    TaskStreamFrame,
    delta_frame,
    done_frame,
    status_frame,
)
from hermes.tasks.control_plane.infrastructure.unix_stream_socket import (
    UnixStreamSocketServer,
)

pytestmark = pytest.mark.integration

# UID real del proceso de test (se usa como "authorized")
_MY_UID = os.getuid()

# UID ficticio que NO debería tener acceso
_FOREIGN_UID = _MY_UID + 1 if _MY_UID != 65534 else _MY_UID - 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sock_path(tmp_path: Path) -> str:
    return str(tmp_path / "tasks.sock")


async def _start_server(
    broker: StreamBroker,
    path: str,
    authorized_uid: int = _MY_UID,
) -> tuple[UnixStreamSocketServer, asyncio.Task]:
    """Arranca el servidor en background; devuelve (server, task)."""
    server = UnixStreamSocketServer(
        broker=broker,
        authorized_uid=authorized_uid,
        sock_path=path,
    )
    task = asyncio.create_task(server.serve_forever())
    # Esperar a que el socket esté disponible
    for _ in range(100):
        if Path(path).exists():
            break
        await asyncio.sleep(0.01)
    return server, task


async def _stop_server(server: UnixStreamSocketServer, task: asyncio.Task) -> None:
    """Para el servidor limpiamente, tolerando errores del shutdown de websockets."""
    server.close()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _collect_frames(
    sock_path: str, task_id: UUID, *, timeout: float = 3.0
) -> list[TaskStreamFrame]:
    """Conecta al socket WS y colecta frames hasta DONE o timeout."""
    frames: list[TaskStreamFrame] = []
    uri = f"ws://localhost/ws/tasks/{task_id}"
    async with unix_connect(sock_path, uri=uri) as ws:
        try:
            async with asyncio.timeout(timeout):
                async for raw in ws:
                    frame = TaskStreamFrame.from_jsonl(str(raw))
                    frames.append(frame)
                    if frame.kind.value in ("done", "error"):
                        break
        except TimeoutError:
            pass
    return frames


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPeerCredAuth:
    async def test_socket_is_group_writable(self, tmp_path: Path) -> None:
        """Regresión: ws_serve creaba el socket 0755 (sin write de grupo), así que
        hermes-user (∈ grupo hermes) recibía EACCES al connect(). El adapter ahora
        hace chmod 0o660 → el grupo puede conectar (la auth real es SO_PEERCRED)."""
        import stat as _stat

        broker = StreamBroker()
        path = _sock_path(tmp_path)
        server, task = await _start_server(broker, path)
        try:
            mode = _stat.S_IMODE(os.stat(path).st_mode)
            assert mode & 0o020, f"socket sin write de grupo: {oct(mode)}"
        finally:
            await _stop_server(server, task)

    async def test_authorized_uid_receives_frames(self, tmp_path: Path) -> None:
        """UID autorizado puede conectar y recibir frames."""
        broker = StreamBroker()
        task_id = uuid4()
        path = _sock_path(tmp_path)

        server, srv_task = await _start_server(broker, path, authorized_uid=_MY_UID)

        try:
            async def _publish() -> None:
                await asyncio.sleep(0.05)
                broker.publish(status_frame(task_id=task_id, status="in_progress"))
                broker.close_task(task_id=task_id, outcome="completed")

            asyncio.create_task(_publish())
            frames = await _collect_frames(path, task_id)
        finally:
            await _stop_server(server, srv_task)

        kinds = [f.kind.value for f in frames]
        assert "status" in kinds
        assert "done" in kinds

    async def test_unauthorized_uid_rejected_403(self, tmp_path: Path) -> None:
        """UID no autorizado recibe rechazo (ConnectionClosedError o HTTP 403)."""
        broker = StreamBroker()
        task_id = uuid4()
        path = _sock_path(tmp_path)

        # Autorizar un UID distinto al del proceso actual
        server, srv_task = await _start_server(
            broker, path, authorized_uid=_FOREIGN_UID
        )

        try:
            uri = f"ws://localhost/ws/tasks/{task_id}"
            with pytest.raises(
                (
                    websockets.exceptions.InvalidStatus,
                    websockets.exceptions.ConnectionClosed,
                ),
            ):
                async with unix_connect(path, uri=uri) as _ws:
                    pass  # No debería llegar aquí
        finally:
            await _stop_server(server, srv_task)


class TestTaskIsolation:
    async def test_two_tasks_do_not_mix_chunks(self, tmp_path: Path) -> None:
        """Dos task_ids concurrentes reciben solo sus propios frames."""
        broker = StreamBroker()
        task_a = uuid4()
        task_b = uuid4()
        path = _sock_path(tmp_path)

        server, srv_task = await _start_server(broker, path)

        try:
            async def _publish_both() -> None:
                await asyncio.sleep(0.05)
                broker.publish(delta_frame(task_id=task_a, delta="alpha"))
                broker.publish(delta_frame(task_id=task_b, delta="beta"))
                broker.close_task(task_id=task_a, outcome="completed")
                broker.close_task(task_id=task_b, outcome="completed")

            asyncio.create_task(_publish_both())

            frames_a, frames_b = await asyncio.gather(
                _collect_frames(path, task_a),
                _collect_frames(path, task_b),
            )
        finally:
            await _stop_server(server, srv_task)

        task_ids_in_a = {str(f.task_id) for f in frames_a}
        task_ids_in_b = {str(f.task_id) for f in frames_b}

        assert str(task_a) in task_ids_in_a
        assert str(task_b) not in task_ids_in_a

        assert str(task_b) in task_ids_in_b
        assert str(task_a) not in task_ids_in_b


class TestReAttach:
    async def test_reattach_replays_response_deltas(
        self, tmp_path: Path
    ) -> None:
        """Re-attach: reconectar a una tarea terminada REPLAYA status+deltas+done.

        Antes re-attach descartaba los deltas → un suscriptor tardío (el ChatWorker
        de Lumen, que se engancha tras completarse una respuesta rápida) recibía
        status+done vacío y la UI no mostraba NINGUNA respuesta. El broker ahora
        acumula los deltas y los replaya para que la respuesta se renderice.
        """
        broker = StreamBroker()
        task_id = uuid4()
        path = _sock_path(tmp_path)

        server, srv_task = await _start_server(broker, path)

        try:
            # Completar la tarea ANTES de que el cliente conecte
            broker.publish(status_frame(task_id=task_id, status="in_progress"))
            broker.publish(delta_frame(task_id=task_id, delta="token_1"))
            broker.publish(delta_frame(task_id=task_id, delta="token_2"))
            broker.publish(delta_frame(task_id=task_id, delta="token_3"))
            broker.close_task(task_id=task_id, outcome="completed")

            # Re-attach: el cliente conecta DESPUÉS de que terminó
            await asyncio.sleep(0.05)
            frames = await _collect_frames(path, task_id)
        finally:
            await _stop_server(server, srv_task)

        kinds = [f.kind.value for f in frames]

        assert "done" in kinds, "Re-attach debe recibir frame DONE"
        # La respuesta (deltas) DEBE replayarse, en orden, para que la UI la muestre.
        deltas = [f.payload.get("delta") for f in frames if f.kind.value == "delta"]
        assert deltas == ["token_1", "token_2", "token_3"], (
            f"Re-attach debe replayar los deltas de la respuesta en orden, got {deltas}"
        )
        assert kinds[-1] == "done", "DONE debe ir al final"

    async def test_reattach_includes_status_before_done(
        self, tmp_path: Path
    ) -> None:
        """Re-attach incluye el status actual antes del done."""
        broker = StreamBroker()
        task_id = uuid4()
        path = _sock_path(tmp_path)

        server, srv_task = await _start_server(broker, path)

        try:
            broker.publish(status_frame(task_id=task_id, status="completed"))
            broker.close_task(task_id=task_id, outcome="completed")

            await asyncio.sleep(0.05)
            frames = await _collect_frames(path, task_id)
        finally:
            await _stop_server(server, srv_task)

        kinds = [f.kind.value for f in frames]
        assert kinds == ["status", "done"], (
            f"Re-attach debe entregar [status, done] en orden, pero fue {kinds}"
        )


class TestBackPressure:
    async def test_slow_client_drops_deltas_without_blocking_broker(
        self, tmp_path: Path
    ) -> None:
        """Back-pressure: broker no bloquea cuando el cliente es lento.

        Publica más deltas que el buffer del suscriptor (64); el broker no
        debe bloquearse. El cliente recibe los que caben + done.
        """
        broker = StreamBroker()
        task_id = uuid4()
        path = _sock_path(tmp_path)

        server, srv_task = await _start_server(broker, path)

        try:
            publish_elapsed: list[float] = []

            async def _flood_publish() -> None:
                await asyncio.sleep(0.05)
                start = time.monotonic()
                for i in range(200):
                    broker.publish(delta_frame(task_id=task_id, delta=f"tok{i}"))
                publish_elapsed.append(time.monotonic() - start)
                broker.close_task(task_id=task_id, outcome="completed")

            asyncio.create_task(_flood_publish())
            frames = await _collect_frames(path, task_id, timeout=5.0)
        finally:
            await _stop_server(server, srv_task)

        # El broker no debe bloquearse publicando 200 frames
        assert publish_elapsed, "El publisher no corrió"
        assert publish_elapsed[0] < 1.0, (
            f"Broker bloqueado durante {publish_elapsed[0]:.3f}s publicando deltas"
        )

        # El frame DONE siempre llega (lifecycle frames nunca se descartan)
        kinds = [f.kind.value for f in frames]
        assert "done" in kinds, "Frame DONE debe llegar aunque se descarten deltas"

        # Algunos deltas pueden haberse descartado (back-pressure)
        delta_count = kinds.count("delta")
        assert delta_count < 200, (
            "Si llegaron todos los deltas sin descartar, el back-pressure no funcionó"
        )
