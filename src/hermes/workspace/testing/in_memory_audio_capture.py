"""InMemoryAudioCapture — fake de AudioCapturePort para tests (T008, US1).

Constitución V: cualquier puerto tiene un fake en testing/ que corre sin
VM, sin device de audio, sin parec.

Emite los chunks configurados en la construcción en secuencia.
Si no se configura ninguno, emite un chunk de silencio de 0.5 s.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

from hermes.workspace.domain.ports.audio_capture_port import (
    AudioChunk,
    AudioCapturePort,
    AudioDeviceUnavailable,
    CaptureState,
    MicrophonePermissionDenied,
)

__all__ = ["InMemoryAudioCapture"]

_SILENCE_CHUNK = bytes(24000)  # 0.5 s a 24 kHz mono int16


class InMemoryAudioCapture:
    """Fake de AudioCapturePort.

    En tests: inyectar chunks via ``preset_chunks`` o ``emit_chunk()``.
    """

    def __init__(
        self,
        *,
        preset_chunks: list[bytes] | None = None,
        deny_permission: bool = False,
        device_unavailable: bool = False,
    ) -> None:
        self._preset_chunks: list[bytes] = preset_chunks or [_SILENCE_CHUNK]
        self._deny_permission = deny_permission
        self._device_unavailable = device_unavailable
        self._state = CaptureState.IDLE
        self._training_session_id: UUID | None = None
        self._tenant_id: UUID | None = None
        self._extra_chunks: list[bytes] = []
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def open(
        self,
        *,
        training_session_id: UUID,
        tenant_id: UUID,
    ) -> None:
        if self._deny_permission:
            raise MicrophonePermissionDenied("Fake: permiso denegado")
        if self._device_unavailable:
            raise AudioDeviceUnavailable("Fake: device no disponible")
        self._training_session_id = training_session_id
        self._tenant_id = tenant_id
        self._state = CaptureState.CAPTURING
        # Pre-cargar los chunks configurados.
        for c in self._preset_chunks:
            await self._queue.put(c)
        await self._queue.put(None)  # sentinel

    def chunks(self) -> AsyncIterator[AudioChunk]:
        return self._chunk_iterator()

    async def _chunk_iterator(self) -> AsyncIterator[AudioChunk]:
        assert self._training_session_id is not None
        assert self._tenant_id is not None
        offset_ms = 0
        while True:
            data = await self._queue.get()
            if data is None:
                break
            duration_ms = int(len(data) / (24000 * 1 * 2) * 1000)
            yield AudioChunk(
                chunk_id=uuid4(),
                training_session_id=self._training_session_id,
                tenant_id=self._tenant_id,
                audio_bytes=data,
                sample_rate_hz=24000,
                channels=1,
                start_offset_ms=offset_ms,
                end_offset_ms=offset_ms + duration_ms,
            )
            offset_ms += duration_ms

    async def close(self) -> None:
        self._state = CaptureState.CLOSED
        # Drena la cola.
        while not self._queue.empty():
            self._queue.get_nowait()

    @property
    def state(self) -> CaptureState:
        return self._state

    def emit_chunk(self, data: bytes) -> None:
        """Añade un chunk al stream (llama desde el test)."""
        self._queue.put_nowait(data)

    def end_stream(self) -> None:
        """Termina el stream (sentinel)."""
        self._queue.put_nowait(None)
