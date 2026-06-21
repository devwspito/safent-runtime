"""WhisperWorker — transcripción local (research §11).

Spec 003 FR-018 — TODA la transcripción del training (US2) ocurre on-
device con faster-whisper distil-large-v3. Cero red al transcribir.

Esta clase es la capa application:
  - cola interna de jobs (FIFO + prioridad opcional).
  - estados: QUEUED → RUNNING → DONE / FAILED.
  - el worker real `FasterWhisperBackend` está en infra (lazy import,
    no obligatorio en CI).
  - los audios crudos NUNCA tocan disco persistente fuera de
    /var/lib/hermes/audio-buffers/<session>/ (tmpfs hint).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from queue import Empty, Queue
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


class TranscriptionState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TranscriptionJob:
    """Job declarativo — sin estado runtime."""

    job_id: UUID
    audio_path: Path
    language_hint: str  # 'es', 'en', 'auto'
    training_session_id: UUID | None
    queued_at: datetime
    priority: int = 0  # mayor = antes


@dataclass(slots=True)
class TranscriptionResult:
    job_id: UUID
    state: TranscriptionState
    text: str | None = None
    detected_language: str | None = None
    duration_seconds: float | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@runtime_checkable
class WhisperBackendPort(Protocol):
    """Backend de transcripción (faster-whisper real, fake en tests)."""

    def transcribe(
        self,
        *,
        audio_path: Path,
        language_hint: str,
    ) -> tuple[str, str, float]:
        """Devuelve (text, detected_language, duration_seconds)."""
        ...


class FakeWhisperBackend:
    """Backend deterministico para tests."""

    def __init__(self, *, response_map: dict[str, str] | None = None) -> None:
        self._map = response_map or {}

    def transcribe(
        self,
        *,
        audio_path: Path,
        language_hint: str,
    ) -> tuple[str, str, float]:
        text = self._map.get(audio_path.name, f"fake transcription of {audio_path.name}")
        lang = language_hint if language_hint != "auto" else "es"
        return text, lang, 12.3


class WhisperWorker:
    """Procesa jobs en background (thread).

    Para CI / tests: `process_one()` sincrónico permite testear sin
    arrancar el thread.
    """

    def __init__(self, *, backend: WhisperBackendPort) -> None:
        self._backend = backend
        self._queue: Queue[TranscriptionJob] = Queue()
        self._results: dict[UUID, TranscriptionResult] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(
        self,
        *,
        audio_path: Path,
        language_hint: str = "auto",
        training_session_id: UUID | None = None,
        priority: int = 0,
    ) -> TranscriptionJob:
        job = TranscriptionJob(
            job_id=uuid4(),
            audio_path=audio_path,
            language_hint=language_hint,
            training_session_id=training_session_id,
            queued_at=datetime.now(tz=UTC),
            priority=priority,
        )
        with self._lock:
            self._results[job.job_id] = TranscriptionResult(
                job_id=job.job_id, state=TranscriptionState.QUEUED
            )
            self._queue.put(job)
        return job

    def get_result(self, *, job_id: UUID) -> TranscriptionResult:
        with self._lock:
            if job_id not in self._results:
                raise KeyError(f"unknown job {job_id}")
            return self._results[job_id]

    def cancel(self, *, job_id: UUID) -> None:
        with self._lock:
            res = self._results.get(job_id)
            if res is None:
                return
            if res.state == TranscriptionState.QUEUED:
                res.state = TranscriptionState.CANCELLED

    def process_one(self, *, timeout: float = 0.1) -> TranscriptionResult | None:
        """Procesa un job — bloqueante hasta timeout."""
        try:
            job = self._queue.get(timeout=timeout)
        except Empty:
            return None
        with self._lock:
            res = self._results[job.job_id]
            if res.state == TranscriptionState.CANCELLED:
                return res
            res.state = TranscriptionState.RUNNING
            res.started_at = datetime.now(tz=UTC)
        try:
            text, lang, dur = self._backend.transcribe(
                audio_path=job.audio_path,
                language_hint=job.language_hint,
            )
            with self._lock:
                res.state = TranscriptionState.DONE
                res.text = text
                res.detected_language = lang
                res.duration_seconds = dur
                res.completed_at = datetime.now(tz=UTC)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                res.state = TranscriptionState.FAILED
                res.error = str(exc)
                res.completed_at = datetime.now(tz=UTC)
        return res

    def start_background(self) -> None:
        """Arranca el thread worker (idempotente)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                self.process_one(timeout=0.25)

        t = threading.Thread(target=_loop, name="whisper-worker", daemon=True)
        self._thread = t
        t.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def queue_depth(self) -> int:
        return self._queue.qsize()
