"""Tests del ClickIntentClassifier (T108) — matriz de 20+ patrones irreversibles.

Capa 3 del wrapper read-only.
"""

from __future__ import annotations

import pytest

from hermes.autonomous.application.click_intent_classifier import ClickIntentClassifier

pytestmark = pytest.mark.unit


@pytest.fixture
def classifier() -> ClickIntentClassifier:
    return ClickIntentClassifier()


class TestDefaultIrreversiblePatterns:
    """Matriz de patrones irreversibles por defecto (FR-020 (e))."""

    @pytest.mark.parametrize(
        "element_text",
        [
            "Eliminar",
            "eliminar",
            "ELIMINAR",
            "Borrar",
            "Confirmar pago",
            "Presentar definitivo",
            "Sí, eliminar",
            "Sí eliminar",
            "Submit",
            "SUBMIT",
            "Aceptar pago",
            "Delete",
            "DELETE",
            "Confirm delete",
            "Permanently delete",
            "No se puede deshacer",
            "Confirmar y enviar",
            "Enviar definitivamente",
            "Finalizar y presentar",
            "Aceptar condiciones",
            "Accept and submit",
            "Pagar ahora",
            "Complete purchase",
            "Place order",
            "Confirm order",
            "Approve",
            "Confirmar baja",
            "Dar de baja",
            "Cancelar contrato",
        ],
    )
    def test_irreversible_pattern_detected(
        self, classifier: ClickIntentClassifier, element_text: str
    ) -> None:
        result = classifier.classify(element_text=element_text)
        assert result.is_irreversible, (
            f"'{element_text}' debe ser detectado como irreversible"
        )

    @pytest.mark.parametrize(
        "element_text",
        [
            "Guardar",
            "Siguiente",
            "Anterior",
            "Buscar",
            "Ver más",
            "Aceptar",  # sin "pago" → no irreversible
            "Ver detalle",
            "Editar",
            "Añadir",
            "Cancelar",  # sin "contrato"
            "Cerrar",
            "Volver",
            "Descargar PDF",
            "OK",
        ],
    )
    def test_reversible_action_not_flagged(
        self, classifier: ClickIntentClassifier, element_text: str
    ) -> None:
        result = classifier.classify(element_text=element_text)
        assert not result.is_irreversible, (
            f"'{element_text}' NO debe ser detectado como irreversible"
        )


class TestAriaLabelAndDataAction:
    def test_detects_via_aria_label(self, classifier: ClickIntentClassifier) -> None:
        result = classifier.classify(
            element_text="",
            aria_label="Eliminar elemento",
        )
        assert result.is_irreversible

    def test_detects_via_data_action(self, classifier: ClickIntentClassifier) -> None:
        result = classifier.classify(
            element_text="",
            data_action="confirm-delete",
        )
        assert result.is_irreversible

    def test_safe_aria_label_not_flagged(self, classifier: ClickIntentClassifier) -> None:
        result = classifier.classify(
            element_text="",
            aria_label="Guardar formulario",
        )
        assert not result.is_irreversible


class TestCustomPatterns:
    def test_extra_pattern_added(self) -> None:
        custom = ClickIntentClassifier(extra_patterns=[r"\bArchivar\b"])
        result = custom.classify(element_text="Archivar expediente")
        assert result.is_irreversible

    def test_extra_pattern_does_not_affect_safe_text(self) -> None:
        custom = ClickIntentClassifier(extra_patterns=[r"\bEliminarCompleto\b"])
        result = custom.classify(element_text="Guardar")
        assert not result.is_irreversible


class TestMatchedPatternReported:
    def test_matched_pattern_is_reported(self, classifier: ClickIntentClassifier) -> None:
        result = classifier.classify(element_text="Eliminar")
        assert result.matched_pattern is not None
        assert "Eliminar" in result.matched_pattern or "Eliminar" in result.matched_pattern

    def test_no_match_has_none_pattern(self, classifier: ClickIntentClassifier) -> None:
        result = classifier.classify(element_text="Guardar")
        assert result.matched_pattern is None
