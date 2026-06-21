"""PlaceholderPane — honest 'coming next' pane.

Used for sections whose real implementation is built in the fan-out step. Shows
an explicit in-construction state — never a fake list presented as real data.
"""

from __future__ import annotations

from textual.widgets import Static

from hermes.tui.screens.base import Pane


class PlaceholderPane(Pane):
    def __init__(self, pane_id: str, title: str, subtitle: str) -> None:
        self.PANE_ID = pane_id
        self.TITLE = title
        self.SUBTITLE = subtitle
        super().__init__()

    def build(self):
        yield Static(
            "Sección en construcción — se conecta al daemon en el siguiente paso.",
            classes="empty-state",
        )
