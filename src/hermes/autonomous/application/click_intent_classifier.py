"""Capa 3: Click Intent Classifier (T108, research §7 capa 3).

Matchea texto/aria-label/data-action de un elemento contra patrones
irreversibles del dialog_policies del SiteSpec (FR-020 (e)).

Si matchea → la demo se detiene en ese step y el caller emite el evento
de bloqueo hacia el panel.

No tiene dependencia de Playwright en el clasificador puro: opera sobre
texto de atributos. El wrapper lo invoca antes de ejecutar click().
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Patrones irreversibles por defecto (FR-020 (e), dialog_policies default).
_DEFAULT_IRREVERSIBLE_PATTERNS: tuple[str, ...] = (
    r"\bEliminar\b",
    r"\bBorrar\b",
    r"\bConfirmar\s+pago\b",
    r"\bPresentar\s+definitivo\b",
    r"\bSí,?\s+eliminar\b",
    r"\bSubmit\b",
    r"\bAceptar\s+pago\b",
    r"\bDelete\b",
    r"\bConfirm\s+delete\b",
    r"\bPermanently\s+delete\b",
    r"\bNo\s+se\s+puede\s+deshacer\b",
    r"\bConfirmar\s+y\s+enviar\b",
    r"\bEnviar\s+definitivamente\b",
    r"\bFinalizar\s+y\s+presentar\b",
    r"\bAceptar\s+condiciones\b",
    r"\bAccept\s+and\s+submit\b",
    r"\bPagar\s+ahora\b",
    r"\bComplete\s+purchase\b",
    r"\bPlace\s+order\b",
    r"\bConfirm\s+order\b",
    r"\bApprove\b",
    r"\bConfirmar\s+baja\b",
    r"\bDar\s+de\s+baja\b",
    r"\bCancelar\s+contrato\b",
)


@dataclass
class ClickIntentResult:
    is_irreversible: bool
    matched_pattern: str | None = None
    element_text: str = ""


class ClickIntentClassifier:
    """Clasificador de intención de click para preview mode.

    Combina los patrones por defecto con los del SiteSpec del tenant.
    Opera en tiempo O(n·m) sobre texto del elemento.
    """

    def __init__(
        self,
        *,
        extra_patterns: Sequence[str] = (),
    ) -> None:
        all_patterns = list(_DEFAULT_IRREVERSIBLE_PATTERNS) + list(extra_patterns)
        self._compiled: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
            (p, re.compile(p, re.IGNORECASE)) for p in all_patterns
        )

    def classify(
        self,
        *,
        element_text: str,
        aria_label: str = "",
        data_action: str = "",
    ) -> ClickIntentResult:
        """Devuelve ClickIntentResult con is_irreversible=True si matchea algún patrón."""
        candidates = [t for t in (element_text, aria_label, data_action) if t]

        for raw_pattern, compiled in self._compiled:
            for text in candidates:
                if compiled.search(text):
                    logger.warning(
                        "replay_preview_irreversible_click_detected",
                        extra={
                            "element_text": element_text[:100],
                            "matched_pattern": raw_pattern,
                        },
                    )
                    return ClickIntentResult(
                        is_irreversible=True,
                        matched_pattern=raw_pattern,
                        element_text=element_text,
                    )

        return ClickIntentResult(is_irreversible=False)

    def all_patterns(self) -> list[str]:
        """Devuelve todos los patrones activos (útil para auditoría)."""
        return [raw for raw, _ in self._compiled]
