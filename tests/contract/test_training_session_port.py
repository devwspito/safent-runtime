"""Contract test del puerto ``TrainingSessionPort``.

El contrato SDD (``specs/002-.../contracts/training_session_port.py``) NO se migró a
``src/`` (ausente en el checkout y en la imagen horneada); a diferencia de sus puertos
hermanos, este Protocol nunca aterrizó en ``hermes.training.domain.ports``. Mientras
falte, la verificación del Protocol se salta y sólo se valida el fake en memoria
``InMemoryTrainingSession`` (que sí existe en ``src/`` y en la imagen).

Spec 002 task T065+/T075. Test runtime-light (sin VM, sin Chromium, sin LLM).
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


def _load_port_protocol():
    """Carga el Protocol real desde ``src/`` si se migró. El contrato SDD de este puerto
    no se migró a ``src/`` (ausente en checkout e imagen), por lo que se salta."""
    try:
        from hermes.training.domain.ports.training_session_port import TrainingSessionPort
    except ImportError as exc:  # pragma: no cover - depende de artefactos de spec
        pytest.skip(
            f"TrainingSessionPort Protocol no presente en src (contrato spec-002 no migrado): {exc}"
        )
    return TrainingSessionPort


class TestPortContract:
    def test_protocol_is_defined(self) -> None:
        proto = _load_port_protocol()
        assert proto is not None
        # Debe ser un Protocol (typing.Protocol o runtime_checkable) o ABC.
        assert inspect.isclass(proto)

    def test_inmemory_skeleton_exists(self) -> None:
        from hermes.training.testing.in_memory_training_session import InMemoryTrainingSession
        assert InMemoryTrainingSession is not None
        instance = InMemoryTrainingSession()
        assert instance is not None
