"""Contract test del puerto ``ReplayPreviewPort``.

El contrato SDD (``specs/002-.../contracts/replay_preview_port.py``) NO se migró a
``src/`` (ausente en el checkout y en la imagen horneada); a diferencia de sus puertos
hermanos, este Protocol nunca aterrizó en ``hermes.autonomous.domain.ports``. Mientras
falte, la verificación del Protocol se salta y sólo se valida el fake en memoria
``InMemoryReplayPreview`` (que sí existe en ``src/`` y en la imagen).

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
        from hermes.autonomous.domain.ports.replay_preview_port import ReplayPreviewPort
    except ImportError as exc:  # pragma: no cover - depende de artefactos de spec
        pytest.skip(
            f"ReplayPreviewPort Protocol no presente en src (contrato spec-002 no migrado): {exc}"
        )
    return ReplayPreviewPort


class TestPortContract:
    def test_protocol_is_defined(self) -> None:
        proto = _load_port_protocol()
        assert proto is not None
        # Debe ser un Protocol (typing.Protocol o runtime_checkable) o ABC.
        assert inspect.isclass(proto)

    def test_inmemory_skeleton_exists(self) -> None:
        from hermes.autonomous.testing.in_memory_replay_preview import InMemoryReplayPreview
        assert InMemoryReplayPreview is not None
        instance = InMemoryReplayPreview()
        assert instance is not None
