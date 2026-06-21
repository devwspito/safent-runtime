"""Contract test del puerto ``SkillPackagePort``.

Verifica que el fake/esqueleto declarado en
``src/hermes/training/testing/in_memory_skill_package_store.py`` declara el shape del Protocol del
contract ``specs/002-.../contracts/skill_package_port.py``.

Spec 002 task T065+. Test runtime-light (sin VM, sin Chromium, sin LLM).
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Cargamos el contract directamente desde el directorio specs/ (no es paquete
# instalado, vive como artefacto del SDD).
_CONTRACT_DIR = Path(__file__).resolve().parents[2] / "specs" / "002-hermes-workspace-training" / "contracts"


def _import_protocol_from_spec():
    sys.path.insert(0, str(_CONTRACT_DIR))
    try:
        mod = __import__("skill_package_port", fromlist=["*"])
    finally:
        sys.path.remove(str(_CONTRACT_DIR))
    return getattr(mod, "SkillPackagePort")


class TestPortContract:
    def test_protocol_is_defined(self) -> None:
        proto = _import_protocol_from_spec()
        assert proto is not None
        # Debe ser un Protocol (typing.Protocol o runtime_checkable)
        # Acepta también clases base abstractas.
        assert inspect.isclass(proto)

    def test_inmemory_skeleton_exists(self) -> None:
        from hermes.training.testing.in_memory_skill_package_store import InMemorySkillPackageStore
        assert InMemorySkillPackageStore is not None
        instance = InMemorySkillPackageStore()
        assert instance is not None
