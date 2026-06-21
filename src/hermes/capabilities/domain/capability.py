"""T034 — Capability VO local del BC capabilities.

Re-exporta `Capability` y `RiskLevel` desde sus fuentes canónicas para que
el resto de la capa `capabilities/` los importe desde un único punto interno
sin crear dependencias circulares.

`Capability` vive en `agents_os/application/consent_manager.py` (spec 003).
`RiskLevel` vive en `capabilities/domain/ports.py` (spec 005).
"""

from __future__ import annotations

from hermes.agents_os.application.consent_manager import Capability, ConsentScope
from hermes.capabilities.domain.ports import RiskLevel

__all__ = ["Capability", "ConsentScope", "RiskLevel"]
