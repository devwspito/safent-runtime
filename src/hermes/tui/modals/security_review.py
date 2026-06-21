"""SecurityReviewModal — the Security Center install gate (scan → score → decide).

Pops on InstallReviewRequested: shows the scan score + verdict + risks for a thing
the agent (or you) is about to install, and records your decision via
RecordInstallDecision. This is the antivirus-style gate: nothing installs without
passing through here. The Cerebro reads the same score to decide how to proceed.
"""

from __future__ import annotations

import json

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from hermes.tui.bridge import RuntimeBridge
from hermes.tui.theme import PALETTE


def _score_color(score: int) -> str:
    if score < 0:
        return PALETTE["text_muted"]
    if score >= 80:
        return PALETTE["success"]
    if score >= 50:
        return PALETTE["warning"]
    return PALETTE["error"]


class SecurityReviewModal(ModalScreen[bool]):
    BINDINGS = [
        ("i", "install", "Instalar"),
        ("escape", "cancel", "Cancelar"),
    ]

    def __init__(self, bridge: RuntimeBridge, scan_id: str, scan_data_json: str) -> None:
        super().__init__()
        self._bridge = bridge
        self._scan_id = scan_id
        try:
            self._d = json.loads(scan_data_json) if scan_data_json else {}
        except json.JSONDecodeError:
            self._d = {}
        self._busy = False

    def _score(self) -> int:
        try:
            return int(self._d.get("score", -1))
        except (TypeError, ValueError):
            return -1

    def _risks(self) -> list:
        r = self._d.get("risks") or self._d.get("findings") or []
        return r if isinstance(r, list) else []

    def compose(self) -> ComposeResult:
        score = self._score()
        verdict = str(self._d.get("verdict", "—"))
        danger = score >= 0 and score < 50 or verdict.lower() in ("fail", "blocked", "block")
        with VerticalScroll(classes="modal-card danger" if danger else "modal-card"):
            title = Text("⛨ Centro de seguridad — revisar instalación",
                         style=f"bold {PALETTE['error'] if danger else PALETTE['amber']}")
            yield Static(title, classes="modal-title")

            what = f"{self._d.get('kind', 'paquete')}: {self._d.get('identifier', '—')}"
            yield Static(self._field("Qué", what))

            sc = Text()
            sc.append("Score: ", style=PALETTE["text_muted"])
            sc.append(f" {score if score >= 0 else '—'}/100 ", style=f"bold {PALETTE['bg']} on {_score_color(score)}")
            sc.append(f"   veredicto: {verdict}", style=PALETTE["text_muted"])
            yield Static(sc, classes="modal-field")

            risks = self._risks()
            if risks:
                body = "\n".join(f"  • {str(r.get('title') if isinstance(r, dict) else r)}" for r in risks[:8])
                yield Static(self._field("Riesgos", body))
            else:
                yield Static(Text("Sin riesgos detectados.", style=PALETTE["success"]), classes="modal-field")

            with Horizontal(classes="modal-actions"):
                yield Button("Cancelar  [esc]", id="sr-cancel")
                yield Button("Instalar  [i]", id="sr-install",
                             classes="-danger" if danger else "-primary")

    def _field(self, label: str, value: str) -> Text:
        t = Text()
        t.append(f"{label}\n", style=PALETTE["text_muted"])
        t.append(value, style=PALETTE["text"])
        return t

    @on(Button.Pressed, "#sr-install")
    def _install_btn(self) -> None:
        self.action_install()

    @on(Button.Pressed, "#sr-cancel")
    def _cancel_btn(self) -> None:
        self.action_cancel()

    def action_install(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.run_worker(self._decide("installed"), exclusive=True)

    def action_cancel(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.run_worker(self._decide("cancelled"), exclusive=True)

    async def _decide(self, decision: str) -> None:
        try:
            await self._bridge.record_install_decision(
                self._scan_id,
                decision,
                identifier=str(self._d.get("identifier", "")),
                kind=str(self._d.get("kind", "")),
                score=self._score(),
                verdict=str(self._d.get("verdict", "")),
                risks_json=json.dumps(self._risks()),
            )
            self.app.notify(
                "Instalación autorizada" if decision == "installed" else "Instalación cancelada",
                timeout=3,
            )
            self.dismiss(decision == "installed")
        except Exception as exc:  # noqa: BLE001
            self._busy = False
            self.app.notify(f"No se pudo registrar la decisión: {exc}", severity="error", timeout=6)
