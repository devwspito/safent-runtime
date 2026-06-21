"""Regression tests: FilesystemSurfaceAdapter.replay_payload (finding #9).

Before this fix, SurfaceReplayPort.replay_payload had zero real
implementations — SkillReplayer could not execute against any real adapter.
This verifies the shim works end-to-end with a real FilesystemSurfaceAdapter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.agents_os.application.skill_replay import (
    ReplayFailurePolicy,
    SkillReplayer,
    SurfaceReplayPort,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
    FilesystemSurfaceAdapter,
)

pytestmark = pytest.mark.unit


class TestFilesystemReplayPayload:
    def test_adapter_satisfies_surface_replay_port(self, tmp_path: Path) -> None:
        """FilesystemSurfaceAdapter satisfies the SurfaceReplayPort Protocol."""
        adapter = FilesystemSurfaceAdapter(
            allowed_prefixes=(str(tmp_path),)
        )
        assert isinstance(adapter, SurfaceReplayPort)

    def test_replay_payload_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        """replay_payload executes write_file and returns True on success."""
        adapter = FilesystemSurfaceAdapter(
            allowed_prefixes=(str(tmp_path),)
        )
        target = tmp_path / "hello.txt"
        payload = {
            "op": "write_file",
            "path": str(target),
            "content": "hello from replay",
        }
        result = adapter.replay_payload(payload)
        assert result is True
        assert target.read_text() == "hello from replay"

    def test_replay_payload_read_file(self, tmp_path: Path) -> None:
        target = tmp_path / "data.txt"
        target.write_text("test data")
        adapter = FilesystemSurfaceAdapter(
            allowed_prefixes=(str(tmp_path),)
        )
        payload = {"op": "read_file", "path": str(target)}
        result = adapter.replay_payload(payload)
        assert result is True

    def test_replay_payload_outside_allowlist_returns_false(self, tmp_path: Path) -> None:
        """replay_payload returns False (not True) when path is outside allowlist."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        adapter = FilesystemSurfaceAdapter(
            allowed_prefixes=(str(allowed),)
        )
        # /tmp is outside the allowed subtree.
        payload = {"op": "read_file", "path": "/tmp/secret.txt"}
        result = adapter.replay_payload(payload)
        assert result is False

    def test_skill_replayer_drives_filesystem_adapter(self, tmp_path: Path) -> None:
        """End-to-end: SkillReplayer executes a signed skill via FilesystemSurfaceAdapter."""
        import secrets  # noqa: PLC0415
        from uuid import uuid4  # noqa: PLC0415

        from hermes.agents_os.application.skill_compiler import (  # noqa: PLC0415
            SkillCompiler,
        )
        from hermes.agents_os.application.training_session_orchestrator import (  # noqa: PLC0415
            TrainingSessionOrchestrator,
        )

        target = tmp_path / "output.txt"
        signing_key = secrets.token_bytes(32)
        compiler = SkillCompiler(signing_key=signing_key)

        # Build a signed skill with one FILESYSTEM step.
        orch = TrainingSessionOrchestrator()
        sess = orch.start(
            tenant_id=uuid4(),
            human_user_id=uuid4(),
            skill_id="write-output",
            surface_kinds_allowed=frozenset([SurfaceKind.FILESYSTEM]),
        )
        orch.capture_step(
            session_id=sess.session_id,
            surface_kind=SurfaceKind.FILESYSTEM,
            action_payload={
                "op": "write_file",
                "path": str(target),
                "content": "replayed",
            },
            voice_caption="escribir archivo",
        )
        orch.request_review(session_id=sess.session_id)
        signed = orch.sign(session_id=sess.session_id, human_confirmed=True)
        package = compiler.compile(session=signed, version=1)

        adapter = FilesystemSurfaceAdapter(
            allowed_prefixes=(str(tmp_path),)
        )
        replayer = SkillReplayer(
            _allow_ungated_replay=True,  # test-only: exercise direct adapter replay
            compiler=compiler,
            adapters_by_surface={SurfaceKind.FILESYSTEM: adapter},
        )
        run = replayer.replay(
            package=package, policy=ReplayFailurePolicy.STOP_ON_FIRST_FAILURE
        )

        assert run.succeeded
        assert target.read_text() == "replayed"
