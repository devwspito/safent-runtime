"""Regression tests for os_native_skills/executors.py (findings #11, #22, #27).

These tests mock the D-Bus / GStreamer boundary so they run in headless CI.
The executors use lazy imports inside function bodies.  The correct patching
target is ``sys.modules`` for the submodules, pre-registered before the
function is called so the ``from <module> import <cls>`` picks up the mock.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_NODE_ID = 42


def _make_source_stub(node_id: int = _NODE_ID) -> MagicMock:
    source = MagicMock(name="MutterScreenCastSource_instance")
    source.primary_connector.return_value = "Virtual-1"
    source.start.return_value = node_id
    return source


# ---------------------------------------------------------------------------
# helpers to inject fake submodules into sys.modules so lazy imports inside
# the executor function bodies resolve to our stubs
# ---------------------------------------------------------------------------


def _inject_fake_mutter(source_stub: MagicMock) -> dict:
    """Return a sys.modules patch dict for mutter_source."""
    fake_mod = MagicMock()
    fake_mod.MutterScreenCastSource = MagicMock(return_value=source_stub)
    return {"hermes.shell_server.screen_capture.mutter_source": fake_mod}


def _inject_fake_gst_capture(cap_stub: MagicMock) -> dict:
    """Return a sys.modules patch dict for gst_capture."""
    fake_mod = MagicMock()
    fake_mod.GstFrameCapture = MagicMock(return_value=cap_stub)
    return {"hermes.shell_server.screen_capture.gst_capture": fake_mod}


def _inject_fake_recorder(recorder_stub: MagicMock) -> dict:
    """Return a sys.modules patch dict for recorder."""
    fake_mod = MagicMock()
    fake_mod.GstScreenRecorder = MagicMock(return_value=recorder_stub)
    return {"hermes.shell_server.screen_capture.recorder": fake_mod}


def _inject_fake_png_writer() -> dict:
    fake_mod = MagicMock()
    fake_mod.encode_rgba_png = MagicMock(return_value=b"PNG")
    return {"hermes.shell_server.training.png_writer": fake_mod}


# ---------------------------------------------------------------------------
# Finding #11 — mutter session must NOT leak when GstFrameCapture.start raises
# ---------------------------------------------------------------------------


class TestExecuteScreenshotSessionLeak:
    """source.stop() must be called even when cap.start() raises (finding #11)."""

    def test_source_stop_called_when_cap_start_raises(self, tmp_path) -> None:
        from hermes.shell_server.screen_capture.domain import CaptureError

        source = _make_source_stub()
        cap_stub = MagicMock(name="GstFrameCapture_instance")
        cap_stub.start.side_effect = CaptureError("pipeline failed")

        patches = {
            **_inject_fake_mutter(source),
            **_inject_fake_gst_capture(cap_stub),
        }

        with patch.dict(sys.modules, patches):
            # Reload executors so the lazy imports inside the functions are not
            # cached from a previous import under sys.modules.
            import importlib

            import hermes.shell_server.os_native_skills.executors as mod

            importlib.reload(mod)

            with (
                patch.object(mod, "_artifact_dir", return_value=tmp_path),
                pytest.raises(CaptureError),
            ):
                mod.execute_screenshot({})

        source.stop.assert_called_once()
        cap_stub.stop.assert_called_once()


class TestExecuteScreenRecordSessionLeak:
    """source.stop() must be called even when recorder.start() raises (finding #11)."""

    def test_source_stop_called_when_recorder_start_raises(self, tmp_path) -> None:
        from hermes.shell_server.screen_capture.domain import CaptureError

        source = _make_source_stub()
        out = tmp_path / "recording_42_5s.webm"
        recorder_stub = MagicMock(name="GstScreenRecorder_instance")
        recorder_stub.start.side_effect = CaptureError("mic not available")
        recorder_stub.stop.return_value = out

        patches = {
            **_inject_fake_mutter(source),
            **_inject_fake_recorder(recorder_stub),
        }

        with patch.dict(sys.modules, patches):
            import importlib

            import hermes.shell_server.os_native_skills.executors as mod

            importlib.reload(mod)

            with (
                patch.object(mod, "_artifact_dir", return_value=tmp_path),
                pytest.raises(CaptureError),
            ):
                mod.execute_screen_record({"duration_seconds": 5})

        source.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Finding #22 — screenshot filenames are unique (no sequence-based collisions)
# ---------------------------------------------------------------------------


class TestScreenshotUniqueFilenameEndToEnd:
    """Two back-to-back execute_screenshot calls must produce distinct paths."""

    def test_two_screenshots_distinct_paths(self, tmp_path) -> None:
        from hermes.shell_server.screen_capture.domain import Frame

        returned_paths: list[str] = []

        for seq in range(2):
            frame = Frame(
                width=4,
                height=4,
                data=bytes([seq + 1]) + bytes(63),
                sequence=seq + 1,
            )
            cap_stub = MagicMock(name="GstFrameCapture_instance")
            cap_stub.latest_frame.return_value = frame
            source = _make_source_stub()

            patches = {
                **_inject_fake_mutter(source),
                **_inject_fake_gst_capture(cap_stub),
                **_inject_fake_png_writer(),
            }

            with patch.dict(sys.modules, patches):
                import importlib

                import hermes.shell_server.os_native_skills.executors as mod

                importlib.reload(mod)

                with patch.object(mod, "_artifact_dir", return_value=tmp_path):
                    result = mod.execute_screenshot({})
                    assert result["ok"] is True
                    returned_paths.append(result["path"])

        assert returned_paths[0] != returned_paths[1], (
            "Two screenshots must not overwrite each other (finding #22)"
        )


# ---------------------------------------------------------------------------
# Finding #27 — screen_record default with_audio is False (secure default)
# ---------------------------------------------------------------------------


class TestScreenRecordAudioDefaultSecure:
    """Omitting with_audio must record video-only (finding #27)."""

    def test_omitting_with_audio_produces_no_audio(self, tmp_path) -> None:
        out = tmp_path / "recording_42_5s.webm"
        out.write_bytes(b"fake_webm_data")

        source = _make_source_stub()
        recorder_stub = MagicMock(name="GstScreenRecorder_instance")
        recorder_stub.stop.return_value = out
        recorder_stub.has_audio = False

        constructed_with: dict = {}

        def recorder_constructor(**kw):
            constructed_with.update(kw)
            return recorder_stub

        fake_recorder_mod = MagicMock()
        fake_recorder_mod.GstScreenRecorder = recorder_constructor

        patches = {
            **_inject_fake_mutter(source),
            "hermes.shell_server.screen_capture.recorder": fake_recorder_mod,
        }

        with patch.dict(sys.modules, patches), patch("time.sleep"):
            import importlib

            import hermes.shell_server.os_native_skills.executors as mod

            importlib.reload(mod)

            with patch.object(mod, "_artifact_dir", return_value=tmp_path):
                result = mod.execute_screen_record({"duration_seconds": 5})

        assert constructed_with.get("with_audio") is False, (
            "screen_record must pass with_audio=False when caller omits the flag"
        )
        assert result["has_audio"] is False

    def test_explicit_with_audio_true_is_respected(self, tmp_path) -> None:
        out = tmp_path / "recording_42_5s.webm"
        out.write_bytes(b"fake_webm_data")

        source = _make_source_stub()
        recorder_stub = MagicMock(name="GstScreenRecorder_instance")
        recorder_stub.stop.return_value = out
        recorder_stub.has_audio = True

        constructed_with: dict = {}

        def recorder_constructor(**kw):
            constructed_with.update(kw)
            return recorder_stub

        fake_recorder_mod = MagicMock()
        fake_recorder_mod.GstScreenRecorder = recorder_constructor

        patches = {
            **_inject_fake_mutter(source),
            "hermes.shell_server.screen_capture.recorder": fake_recorder_mod,
        }

        with patch.dict(sys.modules, patches), patch("time.sleep"):
            import importlib

            import hermes.shell_server.os_native_skills.executors as mod

            importlib.reload(mod)

            with patch.object(mod, "_artifact_dir", return_value=tmp_path):
                mod.execute_screen_record({"duration_seconds": 5, "with_audio": True})

        assert constructed_with.get("with_audio") is True
