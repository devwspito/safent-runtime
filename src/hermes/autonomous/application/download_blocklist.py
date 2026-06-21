"""Capa 2: Download Blocklist (T107, research §7 capa 2).

Cancela descargas de archivos via page.on("download").
Emite evento on_blocked al interceptar.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Download, Page

logger = logging.getLogger(__name__)


class DownloadBlocklist:
    """Cancela descargas en preview mode (FR-020 (c))."""

    def __init__(
        self,
        *,
        on_blocked: Callable[[str], None] | None = None,
    ) -> None:
        self._on_blocked = on_blocked
        self._active = False

    def attach(self, page: "Page") -> None:
        """Registra el handler de download en la Page."""
        page.on("download", self._handle_download)
        self._active = True
        logger.info("download_blocklist_attached")

    def _handle_download(self, download: "Download") -> None:
        url = download.url
        logger.warning(
            "replay_preview_download_blocked",
            extra={"url": url},
        )
        if self._on_blocked:
            self._on_blocked(url)
        # Playwright cancela la descarga si no se llama a save_as().
        # No necesitamos llamar a download.cancel() explícitamente — basta
        # con no guardar el fichero.

    @property
    def is_active(self) -> bool:
        return self._active
