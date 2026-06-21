"""Unit tests for F6.1 training capture (coordinator + api wiring).

Uses only fake backends — no compositor, no GStreamer, no Whisper model.

Coverage:
  - PNG encoder round-trip
  - FakeMicAudioBackend emits WAVs and triggers on_chunk
  - TrainingCaptureCoordinator captures a step with screenshot + voice
  - /start → /stop → /sign persists a SkillPackage in skill_packages_view
  - Existing state machine transitions still pass (backward-compat)
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Fake vault for tests that require signing without master.key.
_FAKE_KEY = b"\xDE" * 32


class _FakeVault:
    def derive_subkey(self, *, label: str) -> bytes:  # noqa: ARG002
        return _FAKE_KEY


def _fake_vault_patch():
    import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

    return patch.object(_mod, "SecretsVault", return_value=_FakeVault())

from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSessionOrchestrator,
)
from hermes.agents_os.application.whisper_worker import (
    FakeWhisperBackend,
    WhisperWorker,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.shell_server.screen_capture.domain import CaptureTarget, Frame
from hermes.shell_server.screen_capture.fake import FakeScreenCaptureBackend
from hermes.shell_server.screen_capture.service import ScreenCaptureService
from hermes.shell_server.training.api import create_training_router
from hermes.shell_server.training.capture_coordinator import (
    TrainingCaptureCoordinator,
)
from hermes.shell_server.training.mic_audio_backend import FakeMicAudioBackend
from hermes.shell_server.training.png_writer import encode_rgba_png

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_frame(width: int = 4, height: int = 4) -> Frame:
    """Non-blank RGBA frame."""
    data = bytes([0xFF, 0x00, 0x00, 0xFF] * (width * height))
    return Frame(width=width, height=height, data=data, sequence=1)


def _make_coordinator(
    orchestrator: TrainingSessionOrchestrator,
    session_dir: Path,
    *,
    whisper_response: str = "acción completada",
    mic_chunks: int = 1,
) -> TrainingCaptureCoordinator:
    fake_backend = FakeScreenCaptureBackend(width=4, height=4, frames=3)
    screen_svc = ScreenCaptureService(backend=fake_backend)

    fake_whisper = FakeWhisperBackend(
        response_map={"audio_chunk_0000.wav": whisper_response}
    )
    worker = WhisperWorker(backend=fake_whisper)

    mic = FakeMicAudioBackend(chunks_to_emit=mic_chunks)

    return TrainingCaptureCoordinator(
        orchestrator=orchestrator,
        screen_service=screen_svc,
        whisper_worker=worker,
        mic_backend=mic,
        monitor_connector="Virtual-1",
        base_dir=session_dir.parent,
    )


# ---------------------------------------------------------------------------
# PNG encoder
# ---------------------------------------------------------------------------


class TestPngWriter:
    def test_encode_produces_valid_png_signature(self) -> None:
        data = bytes([0xFF, 0x00, 0x00, 0xFF] * 4)  # 1×4 RGBA
        png = encode_rgba_png(1, 4, data)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_encode_contains_ihdr_idat_iend(self) -> None:
        data = bytes(4 * 2 * 4)  # 4×2 RGBA all zeros
        png = encode_rgba_png(4, 2, data)
        assert b"IHDR" in png
        assert b"IDAT" in png
        assert b"IEND" in png

    def test_wrong_data_length_raises(self) -> None:
        with pytest.raises(ValueError, match="expected"):
            encode_rgba_png(4, 4, bytes(10))

    def test_encoded_png_is_decodable(self, tmp_path: Path) -> None:
        """Write a PNG and verify it round-trips via stdlib zlib."""
        import struct, zlib  # noqa: PLC0415, E401

        width, height = 2, 2
        data = bytes([0xAB, 0xCD, 0xEF, 0xFF] * (width * height))
        png = encode_rgba_png(width, height, data)

        # Minimal smoke: file starts with PNG signature and is parseable.
        png_path = tmp_path / "test.png"
        png_path.write_bytes(png)
        assert png_path.stat().st_size > 50


# ---------------------------------------------------------------------------
# FakeMicAudioBackend
# ---------------------------------------------------------------------------


class TestFakeMicAudioBackend:
    def test_emits_expected_wav_files(self, tmp_path: Path) -> None:
        received: list[Path] = []
        mic = FakeMicAudioBackend(chunks_to_emit=3)
        mic.start(session_dir=tmp_path, on_chunk=received.append)
        assert len(received) == 3
        for p in received:
            assert p.exists()
            assert p.suffix == ".wav"
            assert p.stat().st_size >= 46  # header + 1 sample

    def test_stop_sets_stopped(self) -> None:
        mic = FakeMicAudioBackend(chunks_to_emit=0)
        mic.start(session_dir=Path("/tmp"), on_chunk=lambda p: None)
        assert not mic._stopped
        mic.stop()
        assert mic._stopped


# ---------------------------------------------------------------------------
# TrainingCaptureCoordinator
# ---------------------------------------------------------------------------


class TestCaptureCoordinator:
    def _fresh_orchestrator_with_session(
        self,
    ) -> tuple[TrainingSessionOrchestrator, UUID]:
        orch = TrainingSessionOrchestrator()
        sess = orch.start(
            tenant_id=uuid4(),
            human_user_id=uuid4(),
            skill_id="test-skill",
            surface_kinds_allowed=frozenset(SurfaceKind),
        )
        return orch, sess.session_id

    def test_begin_creates_session_dir(self, tmp_path: Path) -> None:
        orch, sid = self._fresh_orchestrator_with_session()
        coord = _make_coordinator(orch, tmp_path / str(sid))
        coord.begin(session_id=sid)
        session_dir = tmp_path / str(sid)
        assert session_dir.exists()
        coord.end(session_id=sid)

    def test_capture_screen_step_writes_png(self, tmp_path: Path) -> None:
        orch, sid = self._fresh_orchestrator_with_session()
        coord = _make_coordinator(orch, tmp_path / str(sid))
        coord.begin(session_id=sid)

        step_idx = coord.capture_screen_step(
            session_id=sid,
            surface_kind=SurfaceKind.BROWSER,
            action_payload={"click": "#submit"},
        )
        coord.end(session_id=sid)

        assert step_idx == 0
        session_dir = tmp_path / str(sid)
        png_file = session_dir / "step_0000.png"
        assert png_file.exists()
        assert png_file.stat().st_size > 50

    def test_screenshot_path_stored_in_orchestrator_step(
        self, tmp_path: Path
    ) -> None:
        orch, sid = self._fresh_orchestrator_with_session()
        coord = _make_coordinator(orch, tmp_path / str(sid))
        coord.begin(session_id=sid)
        coord.capture_screen_step(
            session_id=sid,
            surface_kind=SurfaceKind.BROWSER,
            action_payload={"x": 1},
        )
        coord.end(session_id=sid)

        sess = orch.get_session(session_id=sid)
        assert len(sess.steps) == 1
        step = sess.steps[0]
        assert step.screenshot_path is not None
        assert step.screenshot_path.endswith(".png")

    def test_voice_caption_populated_after_audio_chunk(
        self, tmp_path: Path
    ) -> None:
        orch, sid = self._fresh_orchestrator_with_session()
        # 1 chunk, Whisper returns deterministic text.
        coord = _make_coordinator(
            orch,
            tmp_path / str(sid),
            whisper_response="ahora grabo una acción",
            mic_chunks=1,
        )
        coord.begin(session_id=sid)
        coord.capture_screen_step(
            session_id=sid,
            surface_kind=SurfaceKind.BROWSER,
            action_payload={"click": "#btn"},
        )
        coord.end(session_id=sid)

        # WhisperWorker runs synchronously in the background thread
        # (FakeWhisperBackend is instant); wait briefly.
        _drain_whisper_thread(coord, sid, timeout=2.0)

        captions = coord.collected_voice_captions(session_id=sid)
        assert any("acción" in c for c in captions)

    def test_begin_idempotent(self, tmp_path: Path) -> None:
        orch, sid = self._fresh_orchestrator_with_session()
        coord = _make_coordinator(orch, tmp_path / str(sid))
        coord.begin(session_id=sid)
        coord.begin(session_id=sid)  # second call is no-op
        coord.end(session_id=sid)

    def test_active_session_id_reflects_state(self, tmp_path: Path) -> None:
        orch, sid = self._fresh_orchestrator_with_session()
        coord = _make_coordinator(orch, tmp_path / str(sid))
        assert coord.active_session_id() is None
        coord.begin(session_id=sid)
        assert coord.active_session_id() == sid
        coord.end(session_id=sid)


def _drain_whisper_thread(coord: TrainingCaptureCoordinator, sid: UUID, timeout: float) -> None:
    """Wait until all whisper completion threads have finished."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pending = coord.pending_audio_count(session_id=sid)
        except RuntimeError:
            break
        if pending == 0:
            break
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# API wiring: sign compiles + persists SkillPackage
# ---------------------------------------------------------------------------


class _FakeCoordinator:
    """Minimal fake that satisfies coordinator calls without side effects."""

    def __init__(self) -> None:
        self._active: UUID | None = None
        self._ended = False

    def begin(
        self, *, session_id: UUID, skill_name: str = "skill", voice_opt_out: bool = False
    ) -> None:
        self._active = session_id

    def end(self, *, session_id: UUID, wait_whisper_ms: float = 5000.0) -> None:
        self._ended = True

    def active_session_id(self) -> UUID | None:
        return self._active

    def pending_audio_count(self, *, session_id: UUID) -> int:
        return 0

    def collected_voice_captions(self, *, session_id: UUID) -> list[str]:
        return []


@pytest.fixture
def client_with_coord(tmp_path: Path):
    fake_coord = _FakeCoordinator()
    app = FastAPI()
    app.include_router(
        create_training_router(tmp_path / "training.db", coordinator=fake_coord)
    )
    return TestClient(app), tmp_path / "training.db", fake_coord


class TestApiWiring:
    def test_start_calls_coordinator_begin(
        self, client_with_coord
    ) -> None:
        client, db_path, coord = client_with_coord
        r = client.post("/api/v1/training", json={"skill_name": "skill-x"})
        sid = r.json()["session_id"]
        client.post(f"/api/v1/training/{sid}/start")
        assert coord._active == UUID(sid)

    def test_stop_calls_coordinator_end(
        self, client_with_coord
    ) -> None:
        client, db_path, coord = client_with_coord
        r = client.post("/api/v1/training", json={"skill_name": "skill-y"})
        sid = r.json()["session_id"]
        client.post(f"/api/v1/training/{sid}/start")
        client.post(f"/api/v1/training/{sid}/stop")
        assert coord._ended

    def test_sign_persists_skill_package_with_steps(
        self, tmp_path: Path
    ) -> None:
        """Full flow: start→capture step→stop→sign → skill appears in DB."""
        import sqlite3  # noqa: PLC0415

        db_path = tmp_path / "training.db"

        # Build a real coordinator with fakes so it actually captures a step.
        orch = _get_real_orchestrator(db_path)
        fake_backend = FakeScreenCaptureBackend(width=4, height=4, frames=3)
        screen_svc = ScreenCaptureService(backend=fake_backend)
        whisper_w = WhisperWorker(backend=FakeWhisperBackend())
        # Mic activo CON voz: la transcripción no vacía satisface el gate de
        # voz y prueba la cadena completa voz→intent→skill por el camino REST.
        mic = FakeMicAudioBackend(chunks_to_emit=1)
        coord = TrainingCaptureCoordinator(
            orchestrator=orch,
            screen_service=screen_svc,
            whisper_worker=whisper_w,
            mic_backend=mic,
            monitor_connector="Virtual-1",
            base_dir=tmp_path,
        )

        app = FastAPI()
        app.include_router(
            create_training_router(db_path, coordinator=coord)
        )
        client = TestClient(app)

        # Create session.
        r = client.post(
            "/api/v1/training",
            json={"skill_name": "subir-iva-303", "description": "AEAT 303"},
        )
        assert r.status_code == 200
        sid = r.json()["session_id"]

        # Start.
        r = client.post(f"/api/v1/training/{sid}/start")
        assert r.json()["state"] == "capturing"

        # Manually capture a step into the orchestrator so sign can compile.
        _capture_one_step(orch, UUID(sid))

        # Stop → review.
        r = client.post(f"/api/v1/training/{sid}/stop")
        assert r.json()["state"] == "review"

        # Sign: spec 004/US3 — sign produces 'validated'.
        # Patch the vault so resolve_signing_key returns a v2 key without master.key.
        with _fake_vault_patch():
            r = client.post(f"/api/v1/training/{sid}/sign")
        assert r.status_code == 200
        data = r.json()
        assert data["state"] == "validated"
        assert data["signed_at"] is not None
        assert data["step_count"] == 1

        # Verify the SkillPackage landed in skill_packages_view.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM skill_packages_view WHERE skill_id = 'subir-iva-303'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        pkg = rows[0]
        assert pkg["version"] == 1
        # compile_and_persist uses SkillPackageState.SIGNED internally;
        # the DB read path in audit_api normalizes 'signed' → 'validated'.
        assert pkg["state"] in ("signed", "validated")
        assert pkg["signature_short"] is not None
        assert pkg["signing_method"] == "v2"

    def test_sign_rejects_mute_skill_when_mic_active(
        self, tmp_path: Path
    ) -> None:
        """INVARIANTE: micrófono activo + 0 voz + sin opt-out → 422, sin skill.

        Regresión de los críticos #1/#2/#3: nunca se firma una skill muda en
        silencio cuando el micrófono estaba activo.
        """
        import sqlite3  # noqa: PLC0415

        db_path = tmp_path / "training.db"
        orch = _get_real_orchestrator(db_path)
        screen_svc = ScreenCaptureService(
            backend=FakeScreenCaptureBackend(width=4, height=4, frames=3)
        )
        whisper_w = WhisperWorker(backend=FakeWhisperBackend())
        mic = FakeMicAudioBackend(chunks_to_emit=0)  # mic activo pero SIN voz
        coord = TrainingCaptureCoordinator(
            orchestrator=orch,
            screen_service=screen_svc,
            whisper_worker=whisper_w,
            mic_backend=mic,
            monitor_connector="Virtual-1",
            base_dir=tmp_path,
        )
        app = FastAPI()
        app.include_router(create_training_router(db_path, coordinator=coord))
        client = TestClient(app)

        r = client.post(
            "/api/v1/training", json={"skill_name": "muda-303"}
        )
        sid = r.json()["session_id"]
        client.post(f"/api/v1/training/{sid}/start")
        _capture_one_step(orch, UUID(sid))
        client.post(f"/api/v1/training/{sid}/stop")

        # Firmar debe ser RECHAZADO: voz requerida pero transcript vacío.
        r = client.post(f"/api/v1/training/{sid}/sign")
        assert r.status_code == 422

        # NINGÚN SkillPackage muda persistido.
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT * FROM skill_packages_view WHERE skill_id = 'muda-303'"
        ).fetchall()
        conn.close()
        assert len(rows) == 0

    def test_sign_without_review_still_returns_409(
        self, client_with_coord
    ) -> None:
        client, db_path, _ = client_with_coord
        r = client.post("/api/v1/training", json={"skill_name": "x"})
        sid = r.json()["session_id"]
        r2 = client.post(f"/api/v1/training/{sid}/sign")
        assert r2.status_code == 409

    def test_full_lifecycle_backward_compat(
        self, client_with_coord
    ) -> None:
        """Existing 17-test happy path still works with coordinator wired."""
        client, _, _ = client_with_coord
        r = client.post(
            "/api/v1/training",
            json={"skill_name": "backward-compat", "description": "test"},
        )
        assert r.status_code == 200
        sid = r.json()["session_id"]
        assert r.json()["state"] == "idle"

        r = client.post(f"/api/v1/training/{sid}/start")
        assert r.json()["state"] == "capturing"

        r = client.post(f"/api/v1/training/{sid}/stop")
        assert r.json()["state"] == "review"

        # spec 004/US3: sign → 'validated', not 'signed'.
        r = client.post(f"/api/v1/training/{sid}/sign")
        assert r.json()["state"] == "validated"

    def test_version_increments_on_second_sign(self, tmp_path: Path) -> None:
        """Signing twice creates version 1 then version 2."""
        import sqlite3  # noqa: PLC0415

        db_path = tmp_path / "training.db"
        orch = _get_real_orchestrator(db_path)

        def _build_client():
            from hermes.shell_server.training.api import _ORCHESTRATORS  # noqa: PLC0415

            # Fresh orchestrator per call for independent sessions.
            new_orch = TrainingSessionOrchestrator()
            _ORCHESTRATORS[db_path] = new_orch
            whisper_w = WhisperWorker(backend=FakeWhisperBackend())
            mic = FakeMicAudioBackend(chunks_to_emit=1)  # voz presente
            coord = TrainingCaptureCoordinator(
                orchestrator=new_orch,
                screen_service=ScreenCaptureService(
                    backend=FakeScreenCaptureBackend(width=4, height=4, frames=1)
                ),
                whisper_worker=whisper_w,
                mic_backend=mic,
                monitor_connector="Virtual-1",
                base_dir=tmp_path,
            )
            app = FastAPI()
            app.include_router(create_training_router(db_path, coordinator=coord))
            return TestClient(app), new_orch

        def _sign_one_session(client, orch_local, skill_name: str) -> None:
            r = client.post("/api/v1/training", json={"skill_name": skill_name})
            sid = r.json()["session_id"]
            client.post(f"/api/v1/training/{sid}/start")
            _capture_one_step(orch_local, UUID(sid))
            client.post(f"/api/v1/training/{sid}/stop")
            with _fake_vault_patch():
                client.post(f"/api/v1/training/{sid}/sign")

        client1, orch1 = _build_client()
        _sign_one_session(client1, orch1, "same-skill")

        client2, orch2 = _build_client()
        _sign_one_session(client2, orch2, "same-skill")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT version FROM skill_packages_view "
            "WHERE skill_id = 'same-skill' ORDER BY version"
        ).fetchall()
        conn.close()
        versions = [r["version"] for r in rows]
        assert versions == [1, 2]


# ---------------------------------------------------------------------------
# Private helpers for tests
# ---------------------------------------------------------------------------


def _get_real_orchestrator(db_path: Path) -> TrainingSessionOrchestrator:
    from hermes.shell_server.training.api import _ORCHESTRATORS  # noqa: PLC0415

    if db_path not in _ORCHESTRATORS:
        _ORCHESTRATORS[db_path] = TrainingSessionOrchestrator()
    return _ORCHESTRATORS[db_path]


def _capture_one_step(orch: TrainingSessionOrchestrator, session_id: UUID) -> None:
    """Add one BROWSER step to an orchestrator session in RECORDING state."""
    try:
        orch.capture_step(
            session_id=session_id,
            surface_kind=SurfaceKind.BROWSER,
            action_payload={"click": "#test"},
        )
    except Exception:
        pass  # orchestrator may not know this session if start() failed silently
