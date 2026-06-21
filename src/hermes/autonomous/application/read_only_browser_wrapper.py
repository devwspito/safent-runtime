"""ReadOnlyBrowserWrapper — composición de TODAS las capas 1-4 + 6 sub-capas (T105).

Entry point que se enchufa al Playwright BrowserContext al inicio del preview.

Fail-closed (THR-44): si apply() falla, el BrowserContext queda inutilizable
para el preview; el orchestrator debe marcar el estado como FAILED.

Capas:
  1. HttpMethodBlocklist — bloquea PUT/POST/PATCH/DELETE.
  2. DownloadBlocklist — cancela descargas.
  3. ClickIntentClassifier — classifier puro (se invoca desde el orchestrator antes de click).
  4. WebSocketFrameFilter — bloquea frames WS mutadores.
  5-10. ExtendedWrapperLayers — side_effecting_paths, CSP, postMessage, prefetch, form.submit, SW.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from hermes.autonomous.application.click_intent_classifier import ClickIntentClassifier
from hermes.autonomous.application.download_blocklist import DownloadBlocklist
from hermes.autonomous.application.extended_wrapper_layers import ExtendedWrapperLayers
from hermes.autonomous.application.http_method_blocklist import HttpMethodBlocklist
from hermes.autonomous.application.websocket_frame_filter import WebSocketFrameFilter

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)


class WrapperNotActivatedError(RuntimeError):
    """El wrapper read-only no se activó desde el primer frame (constitución IV)."""


class ReadOnlyBrowserWrapper:
    """Composición de todas las capas del wrapper read-only.

    Uso:
        wrapper = ReadOnlyBrowserWrapper(on_mutation_blocked=callback)
        await wrapper.apply(context, page)  # activa ANTES del primer navigate
        # A partir de aquí, el context está en modo read-only.
    """

    def __init__(
        self,
        *,
        on_mutation_blocked: Callable[[str, str], None] | None = None,
        side_effecting_patterns: Sequence[str] = (),
        extra_irreversible_patterns: Sequence[str] = (),
    ) -> None:
        self._on_blocked = on_mutation_blocked
        self._http_blocklist = HttpMethodBlocklist(on_blocked=on_mutation_blocked)
        self._download_blocklist = DownloadBlocklist(
            on_blocked=lambda url: on_mutation_blocked("download", url) if on_mutation_blocked else None
        )
        self._ws_filter = WebSocketFrameFilter(on_blocked=on_mutation_blocked)
        self._extended = ExtendedWrapperLayers(
            side_effecting_patterns=side_effecting_patterns,
            on_blocked=on_mutation_blocked,
        )
        self._click_classifier = ClickIntentClassifier(
            extra_patterns=extra_irreversible_patterns
        )
        self._activated = False

    async def apply(self, context: "BrowserContext", page: "Page") -> None:
        """Aplica todas las capas.

        Fail-closed: si cualquier capa falla, propaga la excepción sin silenciar.
        El orchestrator (T104) marcará el estado como FAILED.
        """
        # Capa 1: HTTP method blocklist (context-level).
        await self._http_blocklist.attach(context)
        # Capa 2: Download blocklist (page-level).
        self._download_blocklist.attach(page)
        # Capa 4: WebSocket frame filter (page-level).
        self._ws_filter.attach(page)
        # Capas 5-10: Extended layers (context + page).
        await self._extended.attach_context(context)
        await self._extended.attach_page(page)

        self._activated = True
        logger.info("read_only_browser_wrapper_activated")

    def classify_click(
        self,
        *,
        element_text: str,
        aria_label: str = "",
        data_action: str = "",
    ) -> bool:
        """Interfaz para capa 3: retorna True si el click es irreversible.

        El orchestrator debe llamar esto ANTES de ejecutar click() del step.
        """
        if not self._activated:
            raise WrapperNotActivatedError("Wrapper no activado — aplicar apply() primero")
        result = self._click_classifier.classify(
            element_text=element_text,
            aria_label=aria_label,
            data_action=data_action,
        )
        return result.is_irreversible

    @property
    def is_activated(self) -> bool:
        return self._activated

    def assert_activated(self) -> None:
        """Fail-closed: verifica que el wrapper está activo (constitución IV)."""
        if not self._activated:
            raise WrapperNotActivatedError(
                "ReadOnlyBrowserWrapper no activado. "
                "El preview NO puede proceder sin wrapper activo (constitución IV)."
            )
