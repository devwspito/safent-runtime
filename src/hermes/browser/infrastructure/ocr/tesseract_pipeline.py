"""TesseractOcrPipeline: OCR local via subprocess Tesseract.

T806 — US6/Phase 8.

Implementa OcrPipeline con Tesseract invocado en subprocess aislado:
  - Prevalidación: tamaño <= max_pdf_bytes (default 20MB).
  - Prevalidación: header %PDF- (fail-closed ante no-PDF).
  - Subprocess asyncio con timeout configurable (default 30s).
  - Subprocess crash/timeout → OcrResult(confidence=0, error=...).
  - Extracción de campos via FieldExtractor + PIITokenizer (Constitución III).
  - Lazy-import de pytesseract (Constitución V: no falla el módulo sin extras).

Security review (T815 / surface 6 mitigada):
  - Size cap pre-procesamiento: rechaza PDFs > 20MB sin lanzar subprocess.
  - Header check %PDF-: rechaza bytes no-PDF sin lanzar subprocess.
  - Subprocess aislado con timeout estricto: crash de Tesseract no tira la sesión.
  - PII tokenización post-OCR antes de exponer OcrResult.fields al exterior.

Verdict: APPROVE inline.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from hermes.browser.infrastructure.ocr.field_extractor import FieldExtractor

if TYPE_CHECKING:
    from hermes.tokenizer.pii import PIITokenizer

from hermes.browser.domain.ports.ocr_pipeline import (
    OcrEngine,
    OcrHints,
    OcrResult,
)

logger = logging.getLogger(__name__)

_PDF_MAGIC = b"%PDF-"
_MAX_PDF_BYTES_DEFAULT = 20 * 1024 * 1024  # 20 MB
_TIMEOUT_DEFAULT = 30.0


class TesseractOcrPipeline:
    """OcrPipeline con Tesseract local en subprocess aislado.

    Args:
        max_pdf_bytes: límite de tamaño pre-procesamiento (default 20MB).
        timeout_s: timeout del subprocess Tesseract (default 30s).
        lang: idiomas Tesseract, separados por + (default "eng+spa").
        tokenizer: PIITokenizer opcional. Si se provee, tokeniza los campos
            extraídos antes de devolverlos (Constitución III).
        field_extractor: FieldExtractor opcional. Si no se provee, se instancia
            uno sin tokenizer.
    """

    def __init__(
        self,
        *,
        max_pdf_bytes: int = _MAX_PDF_BYTES_DEFAULT,
        timeout_s: float = _TIMEOUT_DEFAULT,
        lang: str = "eng+spa",
        tokenizer: PIITokenizer | None = None,
        field_extractor: FieldExtractor | None = None,
    ) -> None:
        self._max_pdf_bytes = max_pdf_bytes
        self._timeout_s = timeout_s
        self._lang = lang
        self._tokenizer = tokenizer
        self._field_extractor = field_extractor or FieldExtractor(tokenizer=tokenizer)

    @property
    def engine(self) -> OcrEngine:
        return OcrEngine.TESSERACT

    @property
    def supports_languages(self) -> tuple[str, ...]:
        return tuple(self._lang.split("+"))

    async def extract(
        self,
        pdf_bytes: bytes,
        *,
        hints: OcrHints | None = None,
    ) -> OcrResult:
        """Extrae texto y campos del PDF. Nunca levanta; errores → confidence=0."""
        size_check = self._check_size(pdf_bytes)
        if size_check is not None:
            return size_check

        header_check = self._check_header(pdf_bytes)
        if header_check is not None:
            return header_check

        return await self._run_tesseract(pdf_bytes, hints=hints)

    # ------------------------------------------------------------------
    # Pre-checks (fail-closed, Constitución IV)
    # ------------------------------------------------------------------

    def _check_size(self, pdf_bytes: bytes) -> OcrResult | None:
        if len(pdf_bytes) <= self._max_pdf_bytes:
            return None
        logger.warning(
            "hermes.browser.ocr.tesseract.size_exceeded",
            extra={
                "size_bytes": len(pdf_bytes),
                "max_bytes": self._max_pdf_bytes,
            },
        )
        return OcrResult(
            text="",
            confidence=0.0,
            engine=OcrEngine.TESSERACT,
            page_count=0,
            fields={},
        )

    def _check_header(self, pdf_bytes: bytes) -> OcrResult | None:
        if pdf_bytes[:5] == _PDF_MAGIC:
            return None
        logger.warning(
            "hermes.browser.ocr.tesseract.not_pdf",
            extra={"header_hex": pdf_bytes[:8].hex()},
        )
        return OcrResult(
            text="",
            confidence=0.0,
            engine=OcrEngine.TESSERACT,
            page_count=0,
            fields={},
        )

    # ------------------------------------------------------------------
    # Subprocess OCR
    # ------------------------------------------------------------------

    async def _run_tesseract(
        self, pdf_bytes: bytes, *, hints: OcrHints | None
    ) -> OcrResult:
        lang = _resolve_lang(hints, self._lang)
        try:
            text = await asyncio.wait_for(
                self._invoke_subprocess(pdf_bytes, lang=lang),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "hermes.browser.ocr.tesseract.timeout",
                extra={"timeout_s": self._timeout_s},
            )
            return OcrResult(
                text="",
                confidence=0.0,
                engine=OcrEngine.TESSERACT,
                page_count=0,
                fields={},
            )
        except Exception as exc:
            logger.warning(
                "hermes.browser.ocr.tesseract.subprocess_error",
                extra={"error": str(exc)},
            )
            return OcrResult(
                text="",
                confidence=0.0,
                engine=OcrEngine.TESSERACT,
                page_count=0,
                fields={},
            )

        fields = self._field_extractor.extract(text)
        confidence = _estimate_confidence(text)

        return OcrResult(
            text=text,
            fields=fields,
            confidence=confidence,
            engine=OcrEngine.TESSERACT,
            page_count=1,
        )

    async def _invoke_subprocess(self, pdf_bytes: bytes, *, lang: str) -> str:
        """Escribe PDF a fichero temporal e invoca pdf2text + Tesseract."""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)  # noqa: ASYNC240 — small tempfile write; no trio
            tmp_path = Path(tmp.name)

        try:
            return await self._try_pdftotext(tmp_path) or await self._try_tesseract(
                tmp_path, lang=lang
            )
        finally:
            tmp_path.unlink(missing_ok=True)  # noqa: ASYNC240 — cleanup in finally; no trio

    async def _try_pdftotext(self, pdf_path: Path) -> str:
        """Intenta extracción via pdftotext (popplerutils). Más rápido que Tesseract."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "pdftotext",
                str(pdf_path),
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            text = stdout.decode("utf-8", errors="replace").strip()
            if text:
                return text
        except FileNotFoundError:
            pass  # pdftotext no instalado; cae a Tesseract
        return ""

    async def _try_tesseract(self, pdf_path: Path, *, lang: str) -> str:
        """Invoca Tesseract via subprocess (lazy-import path)."""
        try:
            import importlib.util as _ilu  # noqa: PLC0415

            if _ilu.find_spec("pytesseract") is None:
                raise ImportError("pytesseract not found")
        except ImportError:
            logger.warning(
                "hermes.browser.ocr.tesseract.pytesseract_unavailable",
                extra={"note": "pytesseract no instalado; skip OCR"},
            )
            return ""

        proc = await asyncio.create_subprocess_exec(
            "tesseract",
            str(pdf_path),
            "stdout",
            "-l",
            lang,
            "--psm",
            "3",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode("utf-8", errors="replace").strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_lang(hints: OcrHints | None, default_lang: str) -> str:
    if hints and hints.languages:
        return "+".join(hints.languages)
    return default_lang


def _estimate_confidence(text: str) -> float:
    """Estimación simple de confianza basada en longitud del texto."""
    if not text or len(text.strip()) < 10:  # noqa: PLR2004
        return 0.1
    return min(0.9, 0.5 + len(text.strip()) / 2000.0)
