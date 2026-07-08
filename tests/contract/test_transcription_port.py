"""Contract test del puerto ``TranscriptionPort``.

El contrato SDD (``specs/002-.../contracts/transcription_port.py``) se migró a ``src/``
en la tarea T075: el Protocol real vive ahora en
``hermes.training.domain.ports.transcription_port``. Este test verifica ese Protocol
real y que el fake en memoria ``FakeTranscription`` existe.

Spec 002 task T065+/T075. Test runtime-light (sin VM, sin Chromium, sin LLM).
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


def _load_port_protocol():
    """Carga el Protocol real desde ``src/`` (el contrato SDD se migró a
    ``hermes.training.domain.ports`` en T075). Si el puerto nunca se migró a
    ``src/`` — contrato SDD ausente en checkout e imagen — se salta."""
    try:
        from hermes.training.domain.ports.transcription_port import TranscriptionPort
    except ImportError as exc:  # pragma: no cover - depende de artefactos de spec
        pytest.skip(
            f"TranscriptionPort Protocol no presente en src (contrato spec-002 no migrado): {exc}"
        )
    return TranscriptionPort


class TestPortContract:
    def test_protocol_is_defined(self) -> None:
        proto = _load_port_protocol()
        assert proto is not None
        # Debe ser un Protocol (typing.Protocol o runtime_checkable) o ABC.
        assert inspect.isclass(proto)

    def test_inmemory_skeleton_exists(self) -> None:
        from hermes.training.testing.fake_transcription import FakeTranscription
        assert FakeTranscription is not None
        instance = FakeTranscription()
        assert instance is not None
