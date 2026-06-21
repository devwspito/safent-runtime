"""Pane — base class for every content screen.

A Pane is a widget mounted once inside the app's ContentSwitcher. When it
becomes visible the app calls activate(), which runs refresh_data(). Subclasses
override build() (compose body) and refresh_data() (load from the bridge).

Shared affordances so all panes look and behave the same (the consistency the
QML sweep lacked): title row, subtitle, honest empty-state, error surface.
"""

from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Static

from hermes.tui.bridge import BridgeError, RuntimeBridge


class Pane(Vertical):
    PANE_ID: str = "pane"
    TITLE: str = ""
    SUBTITLE: str = ""

    def __init__(self) -> None:
        super().__init__(id=f"pane-{self.PANE_ID}")
        self._loaded = False

    @property
    def bridge(self) -> RuntimeBridge:
        return self.app.bridge  # type: ignore[attr-defined]

    def compose(self):
        if self.TITLE:
            yield Static(self.TITLE, classes="pane-title")
        if self.SUBTITLE:
            yield Static(self.SUBTITLE, classes="pane-subtitle")
        yield from self.build()

    def build(self):  # noqa: D401 - subclasses yield their body widgets
        """Yield the pane body. Override in subclasses."""
        return iter(())

    async def activate(self) -> None:
        """Called by the app each time the pane is shown."""
        await self.safe_refresh()

    async def safe_refresh(self) -> None:
        try:
            await self.refresh_data()
            self._loaded = True
        except BridgeError as exc:
            self.notify(f"No se pudo cargar: {exc}", severity="error", timeout=6)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Error inesperado: {exc}", severity="error", timeout=6)

    async def refresh_data(self) -> None:
        """Load data from the bridge into widgets. Override in subclasses."""
