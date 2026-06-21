"""Tests de WhisperModelIntegrityChecker — CTRL-10 / THR-31.

Verifica:
- SHA-256 mismatch → WhisperModelTampered + workspace close + AuditEntry.
- Modelo ausente → WhisperModelMissing + workspace close + AuditEntry.
- Digest correcto → ModelIntegrityResult válido + reporta al canal.

Sin VM real, sin modelos reales. Tests con archivo temporal.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.workspace.application.whisper_model_integrity import (
    ModelIntegrityResult,
    WhisperModelIntegrityChecker,
    WhisperModelMissing,
    WhisperModelTampered,
)
from hermes.workspace.testing.in_memory_control_plane_channel import (
    InMemoryControlPlaneChannel,
)

pytestmark = pytest.mark.unit


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_checker(
    *,
    model_path: Path,
    expected_sha256: str,
) -> tuple[WhisperModelIntegrityChecker, InMemoryControlPlaneChannel]:
    channel = InMemoryControlPlaneChannel()
    checker = WhisperModelIntegrityChecker(
        workspace_id=uuid4(),
        tenant_id=uuid4(),
        channel=channel,
        expected_sha256_hex=expected_sha256,
        model_bin_path=model_path,
    )
    return checker, channel


class TestDigestMatch:
    """Digest correcto → no lanza + reporta whisper_model_loaded."""

    async def test_ok_returns_result(self) -> None:
        content = b"fake model binary content for testing"
        expected = _sha256(content)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            model_path = Path(f.name)

        try:
            checker, channel = _make_checker(
                model_path=model_path,
                expected_sha256=expected,
            )
            result = await checker.verify()

            assert isinstance(result, ModelIntegrityResult)
            assert result.tampered is False
            assert result.missing is False
            assert result.sha256_hex == expected
        finally:
            model_path.unlink(missing_ok=True)

    async def test_ok_emits_whisper_model_loaded(self) -> None:
        content = b"another fake model binary"
        expected = _sha256(content)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            model_path = Path(f.name)

        try:
            checker, channel = _make_checker(
                model_path=model_path, expected_sha256=expected
            )
            await checker.verify()

            assert channel.has_command("whisper_model_loaded")
            loaded_cmds = channel.commands_of("whisper_model_loaded")
            assert loaded_cmds[0]["sha256_hex"] == expected
        finally:
            model_path.unlink(missing_ok=True)

    async def test_ok_does_not_close_workspace(self) -> None:
        content = b"model binary ok"
        expected = _sha256(content)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            model_path = Path(f.name)

        try:
            checker, channel = _make_checker(
                model_path=model_path, expected_sha256=expected
            )
            await checker.verify()
            assert not channel.has_command("close_workspace")
        finally:
            model_path.unlink(missing_ok=True)


class TestDigestMismatch:
    """Digest incorrecto → WhisperModelTampered + close + audit."""

    async def test_mismatch_raises(self) -> None:
        content = b"legitimate model"
        wrong_digest = _sha256(b"different content")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            model_path = Path(f.name)

        try:
            checker, channel = _make_checker(
                model_path=model_path, expected_sha256=wrong_digest
            )
            with pytest.raises(WhisperModelTampered):
                await checker.verify()
        finally:
            model_path.unlink(missing_ok=True)

    async def test_mismatch_emits_audit_entry(self) -> None:
        content = b"legitimate model"
        wrong_digest = _sha256(b"tampered")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            model_path = Path(f.name)

        try:
            checker, channel = _make_checker(
                model_path=model_path, expected_sha256=wrong_digest
            )
            with pytest.raises(WhisperModelTampered):
                await checker.verify()

            assert channel.has_command("audit_entry", audit_kind="whisper_model_tampered")
        finally:
            model_path.unlink(missing_ok=True)

    async def test_mismatch_closes_workspace(self) -> None:
        content = b"real model"
        wrong_digest = _sha256(b"hack")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            model_path = Path(f.name)

        try:
            checker, channel = _make_checker(
                model_path=model_path, expected_sha256=wrong_digest
            )
            with pytest.raises(WhisperModelTampered):
                await checker.verify()

            assert channel.has_command("close_workspace")
            close_cmds = channel.commands_of("close_workspace")
            assert close_cmds[0]["reason"] == "whisper_model_tampered"
        finally:
            model_path.unlink(missing_ok=True)

    async def test_mismatch_reports_before_raising(self) -> None:
        """El control plane recibe whisper_model_loaded antes del mismatch."""
        content = b"model data"
        wrong_digest = _sha256(b"evil")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            model_path = Path(f.name)

        try:
            checker, channel = _make_checker(
                model_path=model_path, expected_sha256=wrong_digest
            )
            with pytest.raises(WhisperModelTampered):
                await checker.verify()

            assert channel.has_command("whisper_model_loaded")
        finally:
            model_path.unlink(missing_ok=True)


class TestModelMissing:
    """Modelo ausente → WhisperModelMissing + close + audit."""

    async def test_missing_file_raises(self) -> None:
        missing_path = Path("/nonexistent/path/model.bin")
        checker, _ = _make_checker(
            model_path=missing_path, expected_sha256="a" * 64
        )
        with pytest.raises(WhisperModelMissing):
            await checker.verify()

    async def test_missing_emits_audit_entry(self) -> None:
        missing_path = Path("/nonexistent/path/model.bin")
        checker, channel = _make_checker(
            model_path=missing_path, expected_sha256="a" * 64
        )
        with pytest.raises(WhisperModelMissing):
            await checker.verify()

        assert channel.has_command("audit_entry", audit_kind="whisper_model_missing")

    async def test_missing_closes_workspace(self) -> None:
        missing_path = Path("/nonexistent/path/model.bin")
        checker, channel = _make_checker(
            model_path=missing_path, expected_sha256="a" * 64
        )
        with pytest.raises(WhisperModelMissing):
            await checker.verify()

        assert channel.has_command("close_workspace")


class TestCaseInsensitiveDigestComparison:
    """El digest se compara case-insensitive (hex puede ser upper o lower)."""

    async def test_uppercase_digest_matches(self) -> None:
        content = b"content to hash"
        expected = _sha256(content).upper()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            model_path = Path(f.name)

        try:
            checker, channel = _make_checker(
                model_path=model_path, expected_sha256=expected
            )
            result = await checker.verify()
            assert result.tampered is False
        finally:
            model_path.unlink(missing_ok=True)
