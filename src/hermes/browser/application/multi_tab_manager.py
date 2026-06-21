"""MultiTabManager: gestión de pestañas del navegador en una sesión.

T809 — US6/Phase 8.

Detecta y expone pestañas adicionales creadas durante la navegación
(popups, window.open, links target=_blank). El LLM decide en cuál actuar
en discovery; en replay el step lleva tab_id explícito.

Integración con Playwright:
    tab_manager = MultiTabManager()
    browser_context.on("page", tab_manager.on_new_page)

Constitución V: tests sin Chromium — on_new_page acepta cualquier objeto
con atributo `url` (duck-typing), sin importar Playwright.

Observabilidad: emite eventos structlog:
    - tab_opened{tab_id, url}
    - tab_focused{tab_id}
    - tab_closed{tab_id}
    - tab_inconsistency_detected{missing_tab_id}
"""

from __future__ import annotations

import contextlib
import logging
from uuid import UUID

from hermes.browser.domain.browser_tab import BrowserTab

logger = logging.getLogger(__name__)


class MultiTabManager:
    """Detecta, registra y cierra pestañas del browser context.

    Thread-safety: no requerida (asyncio single-threaded por sesión).
    """

    def __init__(self) -> None:
        self._tabs: dict[UUID, BrowserTab] = {}
        self._page_to_tab: dict[int, UUID] = {}  # id(page) -> tab_id

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    async def on_new_page(self, page: object) -> BrowserTab:
        """Llamado por Playwright context.on('page', ...).

        `page` es un Playwright Page object (duck-typed para tests).
        """
        url = _get_url(page)
        tab = BrowserTab.new(url=url, is_focused=False)
        self._tabs[tab.tab_id] = tab
        self._page_to_tab[id(page)] = tab.tab_id

        _emit_tab_event("tab_opened", tab_id=tab.tab_id, url=url)

        _register_close_callback(page, self._on_page_close, tab.tab_id)

        return tab

    def focus(self, tab_id: UUID) -> None:
        """Marca tab_id como focused y des-foca las demás."""
        if tab_id not in self._tabs:
            raise KeyError(f"tab_id={tab_id} no registrada en MultiTabManager")
        updated: dict[UUID, BrowserTab] = {}
        for tid, tab in self._tabs.items():
            updated[tid] = tab.with_focused(tid == tab_id)
        self._tabs = updated
        _emit_tab_event("tab_focused", tab_id=tab_id)

    def get_focused(self) -> BrowserTab | None:
        """Devuelve la tab con is_focused=True o None si ninguna."""
        for tab in self._tabs.values():
            if tab.is_focused and tab.is_open:
                return tab
        return None

    def all_open(self) -> list[BrowserTab]:
        """Devuelve todas las tabs abiertas ordenadas por apertura."""
        return sorted(
            (t for t in self._tabs.values() if t.is_open),
            key=lambda t: t.opened_at,
        )

    async def close_all(self) -> None:
        """Cierra todas las tabs registradas (llamado en BrowserSession.close())."""
        for tab_id in list(self._tabs):
            self._mark_closed(tab_id)
        _emit_tab_event("all_tabs_closed")

    def detect_inconsistency(self) -> bool:
        """Detecta si la tab focused ya no está abierta (operador cerró tab esperada).

        Returns True si hay inconsistencia (tab focused = closed/absent).
        """
        focused = self.get_focused()
        if focused is None and self._has_registered_tabs():
            logger.warning(
                "hermes.browser.multi_tab.inconsistency",
                extra={
                    "note": "No hay tab focused pero hay tabs registradas",
                    "open_count": len(self.all_open()),
                },
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Private callbacks
    # ------------------------------------------------------------------

    def _on_page_close(self, tab_id: UUID) -> None:
        self._mark_closed(tab_id)

    def _mark_closed(self, tab_id: UUID) -> None:
        if tab_id in self._tabs:
            self._tabs[tab_id] = self._tabs[tab_id].with_closed()
            _emit_tab_event("tab_closed", tab_id=tab_id)

    def _has_registered_tabs(self) -> bool:
        return bool(self._tabs)


# ---------------------------------------------------------------------------
# Duck-typing helpers (Playwright-independent para tests)
# ---------------------------------------------------------------------------


def _get_url(page: object) -> str:
    try:
        url = getattr(page, "url", "")
        return str(url) if url else ""
    except Exception:
        return ""


def _register_close_callback(
    page: object, callback: ..., tab_id: UUID
) -> None:
    """Registra page.on('close') si el objeto lo soporta (Playwright API)."""
    on_method = getattr(page, "on", None)
    if callable(on_method):
        with contextlib.suppress(Exception):
            on_method("close", lambda: callback(tab_id))


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _emit_tab_event(event: str, **kwargs: object) -> None:
    logger.info(
        f"hermes.browser.multi_tab.{event}",
        extra={k: str(v) for k, v in kwargs.items()},
    )
