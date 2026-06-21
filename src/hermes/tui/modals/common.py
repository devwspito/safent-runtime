"""Shared modal screens: ConfirmModal + FormModal.

Reused by every pane so destructive confirms and create/edit forms look and
behave identically. Both render as a centered .modal-card above a dimmed
backdrop (Textual ModalScreen) — the terminal equivalent of a top-layer modal,
so they can never be painted behind the content (the QML z-order bug, by design
impossible here).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from hermes.tui.theme import PALETTE


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancelar"), ("enter", "confirm", "Confirmar")]

    def __init__(
        self,
        title: str,
        message: str,
        *,
        confirm_label: str = "Confirmar",
        cancel_label: str = "Cancelar",
        danger: bool = False,
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label
        self._danger = danger

    def compose(self) -> ComposeResult:
        card_cls = "modal-card danger" if self._danger else "modal-card"
        with VerticalScroll(classes=card_cls):
            yield Static(Text(self._title, style="bold"), classes="modal-title")
            yield Static(self._message, classes="modal-field")
            with Horizontal(classes="modal-actions"):
                yield Button(self._cancel_label, id="cancel")
                yield Button(
                    self._confirm_label,
                    id="confirm",
                    classes="-danger" if self._danger else "-primary",
                )

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.action_confirm()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.action_cancel()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass
class Field:
    key: str
    label: str
    placeholder: str = ""
    value: str = ""
    secret: bool = False
    required: bool = False


class FormModal(ModalScreen[dict | None]):
    """A create/edit form. Returns {key: value} on save, None on cancel."""

    BINDINGS = [("escape", "cancel", "Cancelar")]

    def __init__(
        self,
        title: str,
        fields: list[Field],
        *,
        save_label: str = "Guardar",
        note: str = "",
    ) -> None:
        super().__init__()
        self._title = title
        self._fields = fields
        self._save_label = save_label
        self._note = note

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal-card"):
            yield Static(Text(self._title, style="bold"), classes="modal-title")
            if self._note:
                yield Static(Text(self._note, style=PALETTE["text_muted"]), classes="modal-field")
            for f in self._fields:
                label = f.label + (" *" if f.required else "")
                yield Static(Text(label, style=PALETTE["text_muted"]))
                yield Input(
                    value=f.value,
                    placeholder=f.placeholder,
                    password=f.secret,
                    id=f"field-{f.key}",
                    classes="modal-field",
                )
            with Horizontal(classes="modal-actions"):
                yield Button("Cancelar", id="cancel")
                yield Button(self._save_label, id="save", classes="-primary")

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        values: dict[str, str] = {}
        for f in self._fields:
            val = self.query_one(f"#field-{f.key}", Input).value.strip()
            if f.required and not val:
                self.app.notify(f"«{f.label}» es obligatorio", severity="error", timeout=4)
                self.query_one(f"#field-{f.key}", Input).focus()
                return
            values[f.key] = val
        self.dismiss(values)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
