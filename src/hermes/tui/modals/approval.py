"""ApprovalModal — the amber HITL card, terminal edition.

The security UX: a HIGH-risk action proposed by the agent waits here for the
owner's explicit decision. Approve calls org.hermes.Runtime1.Approve (returns a
single-use token; the daemon re-dispatches the proposal). Reject calls Reject
with a reason. Escape leaves it pending — it never silently approves.

This is the same gate the QML SecurityApprovalCard drives; identical daemon
verbs, identical authorship (sender_uid). Only the rendering is a TUI.
"""

from __future__ import annotations

import json

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from hermes.tui.bridge import BridgeError, RuntimeBridge
from hermes.tui.theme import PALETTE, risk_color


class ApprovalModal(ModalScreen[bool]):
    BINDINGS = [
        ("a", "approve", "Aprobar"),
        ("r", "reject", "Rechazar"),
        ("escape", "defer", "Después"),
    ]

    def __init__(self, bridge: RuntimeBridge, payload_json: str) -> None:
        super().__init__()
        self._bridge = bridge
        try:
            self._p = json.loads(payload_json) if payload_json else {}
        except json.JSONDecodeError:
            self._p = {"tool": "(payload ilegible)", "raw": payload_json}
        self._proposal_id = str(self._p.get("proposal_id", ""))
        self._busy = False

    def compose(self) -> ComposeResult:
        risk = str(self._p.get("risk", "high"))
        rc = risk_color(risk)
        is_danger = risk.lower() in ("high", "critical")

        with Vertical(classes="modal-card danger" if is_danger else "modal-card"):
            title = Text()
            title.append("⛨ Aprobación requerida", style=f"bold {PALETTE['error'] if is_danger else PALETTE['amber']}")
            yield Static(title, classes="modal-title")

            tool = str(self._p.get("tool") or self._p.get("action") or "acción")
            yield Static(self._field("Acción", tool))

            badge = Text()
            badge.append("Riesgo: ", style=PALETTE["text_muted"])
            badge.append(f" {risk.upper()} ", style=f"bold {PALETTE['bg']} on {rc}")
            yield Static(badge, classes="modal-field")

            justification = str(
                self._p.get("justification") or self._p.get("reason") or "—"
            )
            yield Static(self._field("Motivo del agente", justification))

            if self._proposal_id:
                yield Static(
                    self._field("Propuesta", self._proposal_id[:18] + "…"),
                    classes="modal-field",
                )

            yield Static(
                Text(
                    "Rechazar: escribe un motivo abajo (opcional) y pulsa Rechazar.",
                    style=PALETTE["text_faint"],
                ),
                classes="modal-field",
            )
            yield Input(placeholder="Motivo del rechazo (opcional)", id="reject-reason")

            with Horizontal(classes="modal-actions"):
                yield Button("Rechazar  [r]", id="btn-reject", classes="-danger")
                yield Button("Aprobar  [a]", id="btn-approve", classes="-primary")

    def _field(self, label: str, value: str) -> Text:
        t = Text()
        t.append(f"{label}\n", style=PALETTE["text_muted"])
        t.append(value, style=PALETTE["text"])
        return t

    @on(Button.Pressed, "#btn-approve")
    def _approve_btn(self) -> None:
        self.action_approve()

    @on(Button.Pressed, "#btn-reject")
    def _reject_btn(self) -> None:
        self.action_reject()

    def action_approve(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.run_worker(self._do_approve(), exclusive=True)

    def action_reject(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.run_worker(self._do_reject(), exclusive=True)

    def action_defer(self) -> None:
        self.dismiss(False)

    async def _do_approve(self) -> None:
        try:
            await self._bridge.approve(self._proposal_id)
            self.app.notify("Acción aprobada", timeout=3)
            self.dismiss(True)
        except BridgeError as exc:
            self._busy = False
            self.app.notify(f"No se pudo aprobar: {exc}", severity="error", timeout=6)

    async def _do_reject(self) -> None:
        reason = self.query_one("#reject-reason", Input).value.strip() or "rechazado por el operador"
        try:
            await self._bridge.reject(self._proposal_id, reason)
            self.app.notify("Acción rechazada", timeout=3)
            self.dismiss(False)
        except BridgeError as exc:
            self._busy = False
            self.app.notify(f"No se pudo rechazar: {exc}", severity="error", timeout=6)
