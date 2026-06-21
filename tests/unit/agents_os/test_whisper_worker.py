"""Tests WhisperWorker (FR-018 transcripción on-device)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes.agents_os.application.whisper_worker import (
    FakeWhisperBackend,
    TranscriptionState,
    WhisperBackendPort,
    WhisperWorker,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def backend() -> FakeWhisperBackend:
    return FakeWhisperBackend(
        response_map={"hello.wav": "hola mundo", "form.wav": "rellena nombre"}
    )


@pytest.fixture
def worker(backend: FakeWhisperBackend) -> WhisperWorker:
    return WhisperWorker(backend=backend)


class TestSubmit:
    def test_submit_returns_job_in_queued(
        self, worker: WhisperWorker
    ) -> None:
        job = worker.submit(audio_path=Path("/tmp/hello.wav"))
        res = worker.get_result(job_id=job.job_id)
        assert res.state == TranscriptionState.QUEUED

    def test_queue_depth_tracks_submits(
        self, worker: WhisperWorker
    ) -> None:
        for i in range(3):
            worker.submit(audio_path=Path(f"/tmp/{i}.wav"))
        assert worker.queue_depth() == 3


class TestProcessOne:
    def test_process_one_completes(
        self, worker: WhisperWorker
    ) -> None:
        job = worker.submit(audio_path=Path("hello.wav"))
        res = worker.process_one(timeout=0.5)
        assert res is not None
        assert res.state == TranscriptionState.DONE
        assert res.text == "hola mundo"
        assert res.detected_language == "es"

    def test_process_one_handles_backend_failure(self) -> None:
        class _BoomBackend:
            def transcribe(self, *, audio_path: Path, language_hint: str):
                raise RuntimeError("model OOM")

        worker = WhisperWorker(backend=_BoomBackend())
        job = worker.submit(audio_path=Path("x.wav"))
        res = worker.process_one(timeout=0.5)
        assert res is not None
        assert res.state == TranscriptionState.FAILED
        assert "OOM" in (res.error or "")

    def test_process_one_empty_queue_returns_none(
        self, worker: WhisperWorker
    ) -> None:
        res = worker.process_one(timeout=0.1)
        assert res is None

    def test_cancelled_job_not_processed(
        self, worker: WhisperWorker
    ) -> None:
        job = worker.submit(audio_path=Path("hello.wav"))
        worker.cancel(job_id=job.job_id)
        res = worker.process_one(timeout=0.5)
        assert res.state == TranscriptionState.CANCELLED


class TestBackground:
    def test_start_then_stop(
        self, worker: WhisperWorker
    ) -> None:
        worker.start_background()
        worker.submit(audio_path=Path("hello.wav"))
        # Esperar a que procese.
        for _ in range(20):
            if worker.queue_depth() == 0:
                break
            time.sleep(0.05)
        worker.stop()
        assert worker.queue_depth() == 0


class TestLanguageHint:
    def test_auto_language_resolves_to_default(
        self, worker: WhisperWorker
    ) -> None:
        job = worker.submit(audio_path=Path("hello.wav"), language_hint="auto")
        worker.process_one(timeout=0.5)
        res = worker.get_result(job_id=job.job_id)
        assert res.detected_language == "es"

    def test_explicit_hint_passes_through(
        self, worker: WhisperWorker
    ) -> None:
        job = worker.submit(audio_path=Path("hello.wav"), language_hint="en")
        worker.process_one(timeout=0.5)
        res = worker.get_result(job_id=job.job_id)
        assert res.detected_language == "en"
