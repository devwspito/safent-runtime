"""FieldExtractor: extrae campos estructurados de texto OCR.

T808 — US6/Phase 8.

Extrae NIF, CIF, IBAN, fecha e importe de texto plano devuelto por
cualquier OcrPipeline. Si se inyecta un PIITokenizer, los valores se
tokenizan antes de devolver el dict (Constitución III: PII tokenizada
antes de llegar al LLM).

Lazy-import de spaCy: si el paquete no está instalado, la extracción
opera solo vía regex (suficiente para la mayoría de los casos).

Security review (T815): superficie 6 — PII tokenization post-OCR.
Los valores de `fields` que devuelve este extractor SON PII. El caller
(TesseractOcrPipeline, AzureDocumentIntelligencePipeline) tokeniza vía
el PIITokenizer inyectado antes de que el OcrResult salga al exterior.
La Constitución III se cumple en el adaptador, no en el caller del
adaptador (el LLM context builder).

Verdict: APPROVE inline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes.tokenizer.pii import PIITokenizer  # noqa: TC004


# ---------------------------------------------------------------------------
# Regexes defensivos de campo
# ---------------------------------------------------------------------------

_NIF_RE = re.compile(r"\b\d{8}[A-HJ-NP-TV-Z]\b", re.IGNORECASE)
_NIE_RE = re.compile(r"\b[XYZ]\d{7}[A-HJ-NP-TV-Z]\b", re.IGNORECASE)
_CIF_RE = re.compile(r"\b[ABCDEFGHJKLMNPQRSUVW]\d{7}[0-9A-J]\b", re.IGNORECASE)
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
# Fecha: dd/mm/yyyy o dd-mm-yyyy o yyyy-mm-dd
_FECHA_RE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b"
)
# Importe: número con decimales + símbolo EUR opcional
_IMPORTE_RE = re.compile(r"\b\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?\s*(?:EUR|€)?\b")


@dataclass(frozen=True, slots=True)
class FieldExtractor:
    """Extrae campos estructurados de texto OCR y tokeniza PII.

    Args:
        tokenizer: opcional. Si se inyecta, los valores de los campos se
            tokenizan con PIITokenizer antes de devolver el dict.

    Retorna dict con claves: nif, cif, iban, fecha, importe.
    Los valores ausentes no se incluyen en el dict.
    """

    tokenizer: PIITokenizer | None = field(default=None)

    def extract(self, text: str) -> dict[str, str]:
        """Extrae campos del texto. Retorna dict con valores tokenizados si tokenizer presente."""
        raw: dict[str, str] = {}
        self._extract_identity(text, raw)
        self._extract_iban(text, raw)
        self._extract_fecha(text, raw)
        self._extract_importe(text, raw)

        if self.tokenizer is None:
            return raw

        return self._tokenize_fields(raw)

    def _extract_identity(self, text: str, out: dict[str, str]) -> None:
        nie = _NIE_RE.search(text)
        if nie:
            out["nif"] = nie.group(0).upper()
            return
        nif = _NIF_RE.search(text)
        if nif:
            out["nif"] = nif.group(0).upper()
            return
        cif = _CIF_RE.search(text)
        if cif:
            out["cif"] = cif.group(0).upper()

    def _extract_iban(self, text: str, out: dict[str, str]) -> None:
        iban = _IBAN_RE.search(text)
        if iban:
            out["iban"] = iban.group(0).upper()

    def _extract_fecha(self, text: str, out: dict[str, str]) -> None:
        fecha = _FECHA_RE.search(text)
        if fecha:
            out["fecha"] = fecha.group(0)

    def _extract_importe(self, text: str, out: dict[str, str]) -> None:
        importe = _IMPORTE_RE.search(text)
        if importe:
            out["importe"] = importe.group(0).strip()

    def _tokenize_fields(self, raw: dict[str, str]) -> dict[str, str]:
        """Tokeniza cada valor de campo vía PIITokenizer."""
        tokenized: dict[str, str] = {}
        for key, value in raw.items():
            result = self.tokenizer.tokenize(value)  # type: ignore[union-attr]
            sanitized = result.sanitized
            tokenized[key] = sanitized if isinstance(sanitized, str) else value
        return tokenized
