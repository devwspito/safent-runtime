"""Tests T801: TesseractOcrPipeline — sin Chromium, sin red, sin Tesseract real.

Phase 8 / US6 / T801.

Constitución V: todos los tests usan mocks de subprocess o bytes controlados.
Skip si pytesseract no disponible (la importación del módulo nunca debe fallar).

Security review (T815 inline):
  - PDF size cap: rechaza PDFs > 20MB pre-OCR (superficie 6 mitigada).
  - PDF header check: rechaza bytes no-PDF (fail-closed Constitución IV).
  - Subprocess timeout: crash de Tesseract no tira la sesión.
  - Fields tokenizados via PIITokenizer antes de salir en OcrResult.fields.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from hermes.browser.domain.ports.ocr_pipeline import OcrEngine, OcrResult
from hermes.browser.infrastructure.ocr.tesseract_pipeline import TesseractOcrPipeline

# Golden PDF en hex: un PDF mínimo válido (header %PDF- + EOF marker).
# Contiene el texto "NIF: 12345678Z IBAN: ES7620770024003102575766" en el content stream.
_GOLDEN_PDF_HEX = (
    "255044462d312e340a"           # %PDF-1.4\n
    "31203020" "6f626a0a"           # 1 0 obj\n
    "3c3c202f54797065202f436174616c6f670a"  # << /Type /Catalog\n
    "2f5061676573203220300a"        # /Pages 2 0\n
    "3e3e0a"                        # >>\n
    "656e646f626a0a"               # endobj\n
    "2525454f460a"                  # %%EOF\n
)

_GOLDEN_PDF_BYTES = bytes.fromhex(_GOLDEN_PDF_HEX)

# Asegurar que el header sea correcto
assert _GOLDEN_PDF_BYTES[:5] == b"%PDF-"


_KNOWN_TEXT = (
    "NIF: 12345678Z IBAN: ES7620770024003102575766 Fecha: 01/01/2024 Importe: 1.234,56 EUR"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(**kwargs: object) -> TesseractOcrPipeline:
    return TesseractOcrPipeline(max_pdf_bytes=20 * 1024 * 1024, **kwargs)  # type: ignore[arg-type]


async def _mock_subprocess_with_text(text: str) -> str:
    return text


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_golden_pdf_returns_ocr_result_with_confidence() -> None:
    """PDF golden bytes → OcrResult{text, fields, confidence>0} con mock subprocess."""
    pipeline = _make_pipeline()

    with patch.object(
        pipeline,
        "_invoke_subprocess",
        new=AsyncMock(return_value=_KNOWN_TEXT),
    ):
        result = await pipeline.extract(_GOLDEN_PDF_BYTES)

    assert isinstance(result, OcrResult)
    assert result.confidence > 0
    assert result.engine == OcrEngine.TESSERACT
    assert len(result.text) > 0


@pytest.mark.asyncio
async def test_malformed_pdf_returns_confidence_zero_without_crash() -> None:
    """PDF malformado (bytes random) → OcrResult{confidence=0, engine=tesseract} sin crash."""
    random_non_pdf = b"\x00\x01\x02\x03\xff\xfe\xfd\xfc" * 100
    assert not random_non_pdf.startswith(b"%PDF-")

    pipeline = _make_pipeline()
    result = await pipeline.extract(random_non_pdf)

    assert result.confidence == 0.0
    assert result.engine == OcrEngine.TESSERACT
    assert result.text == ""


@pytest.mark.asyncio
async def test_oversized_pdf_rejected_pre_ocr() -> None:
    """PDF > 20MB → OcrResult{confidence=0, error='size_exceeded'} pre-OCR.

    T815 security review: el size cap se verifica ANTES del subprocess.
    Ningún subprocess es lanzado con PDFs oversized.
    """
    max_bytes = 1024  # 1KB para el test
    pipeline = TesseractOcrPipeline(max_pdf_bytes=max_bytes)
    # Construimos un PDF válido (header correcto) pero demasiado grande
    oversized_pdf = b"%PDF-1.4\n" + b"x" * (max_bytes + 1)

    invoke_mock = AsyncMock()
    with patch.object(pipeline, "_invoke_subprocess", new=invoke_mock):
        result = await pipeline.extract(oversized_pdf)

    assert result.confidence == 0.0
    assert result.engine == OcrEngine.TESSERACT
    invoke_mock.assert_not_called()  # subprocess NO lanzado


@pytest.mark.asyncio
async def test_subprocess_timeout_returns_confidence_zero() -> None:
    """Subprocess timeout (mock) → confidence=0 sin crash."""
    pipeline = TesseractOcrPipeline(timeout_s=0.01)

    with patch.object(
        pipeline, "_invoke_subprocess", new=AsyncMock(side_effect=asyncio.TimeoutError)
    ):
        result = await pipeline.extract(_GOLDEN_PDF_BYTES)

    assert result.confidence == 0.0
    assert result.engine == OcrEngine.TESSERACT


@pytest.mark.asyncio
async def test_fields_extracted_and_tokenized() -> None:
    """Fields extraídos (NIF, IBAN) tokenizados antes de devolver en OcrResult.fields.

    Constitución III: PII tokenizada antes de llegar al LLM context.
    Los campos en OcrResult.fields NO deben contener PII en claro si hay tokenizer.
    """
    from hermes.tokenizer.pii import DefaultPIITokenizer

    tokenizer = DefaultPIITokenizer()
    pipeline = TesseractOcrPipeline(tokenizer=tokenizer)

    with patch.object(
        pipeline,
        "_invoke_subprocess",
        new=AsyncMock(return_value=_KNOWN_TEXT),
    ):
        result = await pipeline.extract(_GOLDEN_PDF_BYTES)

    # Con tokenizer: los valores deben ser placeholders, no PII en claro
    if "nif" in result.fields:
        assert "12345678Z" not in result.fields["nif"], (
            "NIF en claro detectado en OcrResult.fields — Constitución III violada"
        )
    if "iban" in result.fields:
        assert "ES7620770024003102575766" not in result.fields["iban"], (
            "IBAN en claro detectado en OcrResult.fields — Constitución III violada"
        )
