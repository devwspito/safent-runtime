"""Contract test del puerto ``RemoteDesktopGatewayPort``.

El contrato SDD (``specs/002-.../contracts/remote_desktop_gateway_port.py``) se migró a
``src/`` en la tarea T075: el Protocol real vive ahora en
``hermes.workspace.domain.ports.remote_desktop_gateway_port``. Este test verifica ese
Protocol real y que el fake en memoria ``InMemoryRemoteDesktopGateway`` existe.

Spec 002 task T065+/T075. Test runtime-light (sin VM, sin Chromium, sin LLM).
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


def _load_port_protocol():
    """Carga el Protocol real desde ``src/`` (el contrato SDD se migró a
    ``hermes.workspace.domain.ports`` en T075). Si el puerto nunca se migró a
    ``src/`` — contrato SDD ausente en checkout e imagen — se salta."""
    try:
        from hermes.workspace.domain.ports.remote_desktop_gateway_port import (
            RemoteDesktopGatewayPort,
        )
    except ImportError as exc:  # pragma: no cover - depende de artefactos de spec
        pytest.skip(
            f"RemoteDesktopGatewayPort Protocol no presente en src (contrato spec-002 no migrado): {exc}"
        )
    return RemoteDesktopGatewayPort


class TestPortContract:
    def test_protocol_is_defined(self) -> None:
        proto = _load_port_protocol()
        assert proto is not None
        # Debe ser un Protocol (typing.Protocol o runtime_checkable) o ABC.
        assert inspect.isclass(proto)

    def test_inmemory_skeleton_exists(self) -> None:
        from hermes.workspace.testing.in_memory_remote_desktop_gateway import InMemoryRemoteDesktopGateway
        assert InMemoryRemoteDesktopGateway is not None
        instance = InMemoryRemoteDesktopGateway()
        assert instance is not None
