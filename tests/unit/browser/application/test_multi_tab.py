"""Tests T802: MultiTabManager — sin Chromium.

Phase 8 / US6 / T802.

Constitución V: tests sin Playwright real. Se usa duck-typing: objetos
MockPage que tienen atributo `url` y método `on`.
"""

from __future__ import annotations

import pytest

from hermes.browser.application.multi_tab_manager import MultiTabManager
from hermes.browser.domain.browser_tab import BrowserTab

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockPage:
    """Duck-type de Playwright Page para tests."""

    def __init__(self, url: str = "https://stub.local/popup") -> None:
        self.url = url
        self._handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:  # type: ignore[type-arg]
        self._handlers.setdefault(event, []).append(handler)

    def simulate_close(self) -> None:
        for handler in self._handlers.get("close", []):
            handler()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_tab_added_to_manager_on_page_event() -> None:
    """Nueva tab abierta vía context.on('page') → BrowserTab añadido + evento tab_opened."""
    manager = MultiTabManager()
    mock_page = MockPage(url="https://stub.local/popup")

    tab = await manager.on_new_page(mock_page)

    assert isinstance(tab, BrowserTab)
    assert tab.url == "https://stub.local/popup"
    assert tab.is_open
    assert len(manager.all_open()) == 1


@pytest.mark.asyncio
async def test_focus_sets_only_one_tab_focused() -> None:
    """LLM decide foco → is_focused=True solo en una tab."""
    manager = MultiTabManager()
    page_a = MockPage(url="https://stub.local/page-a")
    page_b = MockPage(url="https://stub.local/page-b")

    tab_a = await manager.on_new_page(page_a)
    tab_b = await manager.on_new_page(page_b)

    manager.focus(tab_a.tab_id)

    tabs = manager.all_open()
    focused_ids = [t.tab_id for t in tabs if t.is_focused]
    assert len(focused_ids) == 1
    assert focused_ids[0] == tab_a.tab_id

    manager.focus(tab_b.tab_id)

    tabs = manager.all_open()
    focused_ids = [t.tab_id for t in tabs if t.is_focused]
    assert len(focused_ids) == 1
    assert focused_ids[0] == tab_b.tab_id


@pytest.mark.asyncio
async def test_close_all_closes_every_tab() -> None:
    """Cierre de sesión cierra todas las tabs."""
    manager = MultiTabManager()
    page_a = MockPage(url="https://stub.local/a")
    page_b = MockPage(url="https://stub.local/b")

    await manager.on_new_page(page_a)
    await manager.on_new_page(page_b)

    assert len(manager.all_open()) == 2

    await manager.close_all()

    assert len(manager.all_open()) == 0


@pytest.mark.asyncio
async def test_tab_closed_accidentally_detect_inconsistency() -> None:
    """Tab cerrada accidentalmente por operador → detect_inconsistency() True."""
    manager = MultiTabManager()
    page_a = MockPage(url="https://stub.local/a")
    tab_a = await manager.on_new_page(page_a)

    # Focalizar tab_a
    manager.focus(tab_a.tab_id)
    assert manager.get_focused() is not None

    # Simular cierre accidental por operador
    page_a.simulate_close()

    # detect_inconsistency: focused=None pero hay tabs registradas (though closed)
    # La tab cerrada aún está en el registro pero con closed_at set
    # Al cerrar, la tab focused ya no aparece en all_open() → inconsistencia
    inconsistent = manager.detect_inconsistency()
    assert inconsistent
