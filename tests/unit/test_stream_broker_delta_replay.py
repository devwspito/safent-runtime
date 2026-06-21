"""GATE 0 / chat-render 🔒 — re-attach DEBE replayar los deltas (la respuesta).

Bug: un suscriptor que conecta DESPUÉS de que la tarea de chat se completó recibía
solo status+done (sin texto) → la UI mostraba el mensaje del usuario pero ninguna
respuesta. El StreamBroker ahora acumula los deltas y los replaya en re-attach.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from hermes.tasks.control_plane.application.stream_broker import StreamBroker
from hermes.tasks.control_plane.domain.task_stream_frame import (
    delta_frame,
    done_frame,
    status_frame,
)

pytestmark = pytest.mark.unit


async def _collect(broker: StreamBroker, task_id) -> list:
    return [f async for f in broker.subscribe(task_id=task_id)]


def test_reattach_replays_deltas() -> None:
    async def run() -> None:
        broker = StreamBroker()
        tid = uuid4()
        # Simula el ciclo de chat completo ANTES de que nadie se suscriba.
        broker.publish(status_frame(task_id=tid, status="in_progress"))
        broker.publish(delta_frame(task_id=tid, delta="Hola, soy Lumen."))
        broker.close_task(task_id=tid, outcome="completed")
        # Suscriptor TARDÍO (re-attach): debe recibir status + delta + done.
        frames = await asyncio.wait_for(_collect(broker, tid), timeout=2)
        kinds = [f.kind.value for f in frames]
        assert "delta" in kinds, f"re-attach debe incluir delta; got {kinds}"
        deltas = [f.payload.get("delta") for f in frames if f.kind.value == "delta"]
        assert "Hola, soy Lumen." in deltas
        assert kinds[-1] == "done"

    asyncio.run(run())


def test_reattach_multiple_deltas_in_order() -> None:
    async def run() -> None:
        broker = StreamBroker()
        tid = uuid4()
        broker.publish(status_frame(task_id=tid, status="in_progress"))
        for part in ("uno ", "dos ", "tres"):
            broker.publish(delta_frame(task_id=tid, delta=part))
        broker.close_task(task_id=tid, outcome="completed")
        frames = await asyncio.wait_for(_collect(broker, tid), timeout=2)
        deltas = [f.payload.get("delta") for f in frames if f.kind.value == "delta"]
        assert deltas == ["uno ", "dos ", "tres"]

    asyncio.run(run())


def test_live_subscriber_still_gets_deltas() -> None:
    """No-regresión: un suscriptor en vivo sigue recibiendo deltas + done."""
    async def run() -> None:
        broker = StreamBroker()
        tid = uuid4()
        got: list = []

        async def sub() -> None:
            async for f in broker.subscribe(task_id=tid):
                got.append(f.kind.value)

        task = asyncio.create_task(sub())
        await asyncio.sleep(0.05)  # deja que se suscriba
        broker.publish(delta_frame(task_id=tid, delta="hola"))
        broker.close_task(task_id=tid, outcome="completed")
        await asyncio.wait_for(task, timeout=2)
        assert "delta" in got and got[-1] == "done"

    asyncio.run(run())
