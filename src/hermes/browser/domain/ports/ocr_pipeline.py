"""OcrPipeline: puerto de dominio para OCR de PDFs intermedios.

Trasladado del contract canónico (specs/001-stack-browser-brutal/contracts/ocr_pipeline.py)
al dominio del runtime. Las implementaciones concretas viven en:
  - infrastructure/ocr/tesseract_pipeline.py (default, local, sin red)
  - infrastructure/ocr/azure_di_pipeline.py  (opt-in, EU, cloud)
  - testing/fake_ocr_pipeline.py              (tests deterministas)

Constitución III: los campos extraídos pueden ser PII; se tokenizan antes
de llegar al provider LLM. El OCR en sí no tokeniza; lo hace el caller
antes de inyectar el OcrResult como contexto del siguiente step.

Constitución V: TesseractOcrPipeline funciona sin red salida. Azure DI
vive bajo marker `requires_external_ocr`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class OcrError(RuntimeError):
    """Base de errores OCR."""


class OcrEngineUnavailable(OcrError):
    """Engine instalado pero no operativo (Tesseract sin paquete `es`, etc.)."""


class OcrEngine(StrEnum):
    TESSERACT = "tesseract"
    AZURE_DI_EU = "azure_di_eu"
    FAKE = "fake"


@dataclass(frozen=True, slots=True)
class OcrHints:
    """Hints opcionales para mejorar extracción.

    `expected_fields`: nombres canónicos esperados (`nif`, `iban`,
    `importe`, `fecha`, `referencia`, ...).
    `languages`: lista de códigos Tesseract / Azure DI (e.g. ["spa", "eng"]).
    `document_kind`: heurística high-level ("aeat_justificante",
    "extracto_bancario", "factura"...). El adapter puede ignorarlo.
    """

    expected_fields: tuple[str, ...] = ()
    languages: tuple[str, ...] = ("spa", "eng")
    document_kind: str = ""


@dataclass(frozen=True, slots=True)
class OcrResult:
    """Resultado del OCR.

    `text`: texto plano del documento (puede contener PII).
    `fields`: extracción estructurada por nombre canónico ({"nif": "...",
    "iban": "...", ...}). Vacío si no se pudo extraer.
    `confidence`: ∈ [0, 1]. Por debajo del threshold del flow, el caller
    degrada a HITL.
    `engine`: identifica qué motor produjo el resultado para audit.
    `page_count`: nº de páginas del PDF.
    """

    text: str
    fields: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    engine: OcrEngine = OcrEngine.FAKE
    page_count: int = 0
    extracted_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@runtime_checkable
class OcrPipeline(Protocol):
    """Contrato de OCR sobre PDF en bytes."""

    async def extract(
        self,
        pdf_bytes: bytes,
        *,
        hints: OcrHints | None = None,
    ) -> OcrResult:
        """Devuelve OcrResult. Nunca levanta; en error devuelve confidence=0
        y `text=""`, `engine` correcto, y el caller decide degradar.

        El adapter debe ser idempotente: mismo `pdf_bytes` + `hints` →
        mismo OcrResult (útil para tests con golden).
        """
        ...

    @property
    def engine(self) -> OcrEngine:
        """Identifier del motor concreto."""
        ...

    @property
    def supports_languages(self) -> tuple[str, ...]:
        """Idiomas soportados por la instalación actual."""
        ...
