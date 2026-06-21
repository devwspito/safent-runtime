"""AzureDocumentIntelligencePipeline: OCR cloud con Azure DI EU.

T807 — US6/Phase 8.

Implementa OcrPipeline sobre Azure Document Intelligence con verificación
obligatoria de región EU (NC-3 del threat-model).

Controles de seguridad (T815 / NC-3 mitigado):
  - _verify_region: valida que el endpoint pertenece a una región EU antes
    de cualquier llamada. Si region_check=True y el endpoint no es EU →
    RegionViolation (fail-closed, Constitución IV).
  - Audit log en cada llamada: ocr_routed_external{engine, tenant_id,
    document_kind, page_count} SIN el contenido del documento.
  - Lazy-import de azure.ai.documentintelligence (Constitución V).
  - Marker `requires_external_ocr` en los tests que usen endpoint real.

NC-3 Closure: Azure DI EU es OPT-IN. El consumer debe inyectar
  AzureDocumentIntelligencePipeline explícitamente en su composition
  root con enable_azure_di=True. El runtime no lo activa por defecto.

Verdict: APPROVE inline (T815).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from hermes.browser.domain.ports.ocr_pipeline import (
    OcrEngine,
    OcrHints,
    OcrResult,
)
from hermes.browser.infrastructure.ocr.field_extractor import FieldExtractor

if TYPE_CHECKING:
    from uuid import UUID

    from hermes.tokenizer.pii import PIITokenizer  # noqa: TC004

logger = logging.getLogger(__name__)

# Regiones EU aceptadas de Azure Cognitive Services / Document Intelligence.
_EU_REGION_RE = re.compile(
    r"(?:"
    r".*\.francecentral\..*"
    r"|.*\.westeurope\..*"
    r"|.*\.northeurope\..*"
    r"|.*\.swedencentral\..*"
    r"|.*-eu\.api\.cognitive\.microsoft\.com"
    r")",
    re.IGNORECASE,
)


class RegionViolation(RuntimeError):
    """El endpoint Azure DI no pertenece a una región EU autorizada.

    Constitución IV: fail-closed. El caller no debe continuar.
    """

    def __init__(self, endpoint: str) -> None:
        super().__init__(
            f"Azure DI endpoint '{endpoint}' no pertenece a una región EU. "
            "NC-3 (threat-model): el procesado de PII de clientes GDPR-scope "
            "requiere endpoint EU. Operación bloqueada."
        )
        self.endpoint = endpoint


class AzureDocumentIntelligencePipeline:
    """OcrPipeline con Azure DI EU. Opt-in vía configuración del consumer.

    Args:
        endpoint: URL del Azure DI endpoint. Debe ser de región EU si
            region_check=True.
        api_key: clave de API de Azure DI. No aparece en logs.
        region_check: si True (default), verifica región EU al construir.
            Desactivar solo para tests con mocks controlados.
        tokenizer: PIITokenizer opcional para tokenizar campos extraídos.
        audit_log: callable opcional para registrar eventos de enrutamiento.
            Firma: (event_name: str, **kwargs) -> None. Si None, usa structlog.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        region_check: bool = True,
        tokenizer: PIITokenizer | None = None,
        audit_log: Callable[..., None] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._region_check = region_check
        self._tokenizer = tokenizer
        self._field_extractor = FieldExtractor(tokenizer=tokenizer)
        self._audit_log = audit_log or self._default_audit_log

        if region_check:
            self._verify_region(endpoint)

    @property
    def engine(self) -> OcrEngine:
        return OcrEngine.AZURE_DI_EU

    @property
    def supports_languages(self) -> tuple[str, ...]:
        return ("spa", "eng", "fra", "deu", "por", "ita")

    async def extract(
        self,
        pdf_bytes: bytes,
        *,
        hints: OcrHints | None = None,
        tenant_id: UUID | None = None,
    ) -> OcrResult:
        """Extrae texto y campos via Azure DI. Audit log obligatorio sin contenido."""
        document_kind = hints.document_kind if hints else ""
        self._emit_audit(
            tenant_id=str(tenant_id) if tenant_id else "unknown",
            document_kind=document_kind,
            page_count=0,
        )

        try:
            text, page_count = await self._call_azure_di(pdf_bytes, hints=hints)
        except Exception as exc:
            logger.warning(
                "hermes.browser.ocr.azure_di.error",
                extra={"error": str(exc)},
            )
            return OcrResult(
                text="",
                confidence=0.0,
                engine=OcrEngine.AZURE_DI_EU,
                page_count=0,
                fields={},
            )

        fields = self._field_extractor.extract(text)
        confidence = min(0.95, 0.7 + len(text.strip()) / 5000.0) if text else 0.0

        return OcrResult(
            text=text,
            fields=fields,
            confidence=confidence,
            engine=OcrEngine.AZURE_DI_EU,
            page_count=page_count,
        )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _verify_region(self, endpoint: str) -> None:
        netloc = urlparse(endpoint).netloc or endpoint
        if not _EU_REGION_RE.match(netloc):
            raise RegionViolation(endpoint)

    async def _call_azure_di(
        self, pdf_bytes: bytes, *, hints: OcrHints | None = None  # noqa: ARG002
    ) -> tuple[str, int]:
        """Invoca Azure DI. Lazy-import para Constitución V."""
        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient  # noqa: PLC0415
            from azure.ai.documentintelligence.models import AnalyzeDocumentRequest  # noqa: PLC0415
            from azure.core.credentials import AzureKeyCredential  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "azure-ai-documentintelligence no está instalado. "
                "Instala con: pip install hermes-runtime[ocr-cloud]"
            ) from exc

        client = DocumentIntelligenceClient(
            endpoint=self._endpoint,
            credential=AzureKeyCredential(self._api_key),
        )
        poller = await client.begin_analyze_document(
            "prebuilt-read",
            AnalyzeDocumentRequest(bytes_source=pdf_bytes),
        )
        result = await poller.result()

        text_parts: list[str] = []
        for page in result.pages or []:
            for line in page.lines or []:
                text_parts.append(line.content)

        return "\n".join(text_parts), len(result.pages or [])

    def _emit_audit(
        self, *, tenant_id: str, document_kind: str, page_count: int
    ) -> None:
        self._audit_log(
            "ocr_routed_external",
            engine="azure_di_eu",
            tenant_id=tenant_id,
            document_kind=document_kind,
            page_count=page_count,
        )

    @staticmethod
    def _default_audit_log(event: str, **kwargs: object) -> None:
        logger.info(
            f"hermes.browser.ocr.{event}",
            extra={"metric": "browser_ocr_external_routed_total", **kwargs},
        )
