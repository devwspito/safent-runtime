"""StatusBar — the always-on header band.

Brand on the left; live system state on the right (active agent, model,
auto-mode, queue depth, connection, kill-switch). Reactive: the app writes
attributes; the bar repaints itself. No business logic here.
"""

from __future__ import annotations

from rich.text import Text
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Static

from hermes.tui.theme import PALETTE


class StatusBar(Horizontal):
    agent_name: reactive[str] = reactive("Safent")
    model_name: reactive[str] = reactive("—")
    auto_mode: reactive[bool] = reactive(False)
    paused: reactive[bool] = reactive(False)
    connected: reactive[bool] = reactive(False)
    pending: reactive[int] = reactive(0)
    offline: reactive[bool] = reactive(False)

    def compose(self):
        yield Static(id="hdr-left")
        yield Static(id="hdr-right")

    def on_mount(self) -> None:
        self._repaint()

    def watch_agent_name(self) -> None:
        self._repaint()

    def watch_model_name(self) -> None:
        self._repaint()

    def watch_auto_mode(self) -> None:
        self._repaint()

    def watch_paused(self) -> None:
        self._repaint()

    def watch_connected(self) -> None:
        self._repaint()

    def watch_pending(self) -> None:
        self._repaint()

    def watch_offline(self) -> None:
        self._repaint()

    def _repaint(self) -> None:
        try:
            left = self.query_one("#hdr-left", Static)
            right = self.query_one("#hdr-right", Static)
        except Exception:  # noqa: BLE001 — not mounted yet
            return

        brand = Text()
        brand.append("◆ SAFENT", style=f"bold {PALETTE['amber']}")
        brand.append("  ·  ", style=PALETTE["text_faint"])
        crown = "♛ " if self.agent_name.lower() in ("safent", "hermes", "cerebro") else ""
        brand.append(f"{crown}{self.agent_name}", style=PALETTE["text"])
        if crown:
            brand.append("  Cerebro · omnipotente", style=PALETTE["text_faint"])
        left.update(brand)

        seg = Text()
        seg.append(f"{self.model_name}", style=PALETTE["text_muted"])
        seg.append("  ·  ", style=PALETTE["text_faint"])
        if self.auto_mode:
            seg.append("AUTO ●", style=f"bold {PALETTE['amber']}")
        else:
            seg.append("auto ○", style=PALETTE["text_faint"])
        seg.append("  ·  ", style=PALETTE["text_faint"])
        seg.append(f"cola {self.pending}", style=PALETTE["text_muted"])
        seg.append("  ·  ", style=PALETTE["text_faint"])
        if self.offline:
            seg.append("○ sin conexión", style=f"bold {PALETTE['warning']}")
        elif self.connected:
            seg.append("● conectado", style=PALETTE["success"])
        else:
            seg.append("○ conectando…", style=PALETTE["text_muted"])
        if self.paused:
            seg.append("  ·  ", style=PALETTE["text_faint"])
            seg.append("⏸ PAUSADO", style=f"bold {PALETTE['error']}")
        right.update(seg)
