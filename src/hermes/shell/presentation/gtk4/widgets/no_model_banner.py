"""NoModelBanner — banner propio de "conectar modelo" con descarte real.

Reemplaza al Adw.Banner que no soporta dos acciones ni descarte honesto.

Layout: [texto] [Conectar ahora] [Más tarde]
Semántica:
  - "Conectar ahora" → abre ProvidersDialog (callback on_connect).
  - "Más tarde" → oculta el banner y persiste el descarte en LayoutPrefs
    para que no reaparezca en próximos arranques mientras no haya modelo.
  - set_visible(True) solo lo llama window.py; el banner nunca se muestra
    solo una vez descartado (la lógica de guarda vive en window.py).
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


class NoModelBanner(Gtk.Box):
    """Banner horizontal con dos acciones: Conectar ahora y Más tarde."""

    def __init__(
        self,
        *,
        on_connect: Callable[[], None],
        on_dismiss: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.add_css_class("hermes-no-model-banner")
        self.set_hexpand(True)
        # Oculto hasta que window.py confirme que no hay modelo.
        self.set_visible(False)

        self._on_connect = on_connect
        self._on_dismiss = on_dismiss

        self._build()

    def _build(self) -> None:
        label = Gtk.Label(
            label="Tu asistente está casi listo.",
            hexpand=True,
            xalign=0.0,
        )
        label.add_css_class("hermes-no-model-banner-text")
        self.append(label)

        connect_btn = Gtk.Button(label="Conectar ahora")
        connect_btn.add_css_class("hermes-no-model-banner-connect")
        connect_btn.set_tooltip_text("Configura el servicio que usará tu asistente")
        connect_btn.connect("clicked", self._on_connect_clicked)
        self.append(connect_btn)

        dismiss_btn = Gtk.Button(label="Más tarde")
        dismiss_btn.add_css_class("hermes-no-model-banner-dismiss")
        dismiss_btn.set_tooltip_text("Puedes hacerlo desde Ajustes cuando quieras.")
        dismiss_btn.connect("clicked", self._on_dismiss_clicked)
        self.append(dismiss_btn)

    def _on_connect_clicked(self, _btn: Gtk.Button) -> None:
        self._on_connect()

    def _on_dismiss_clicked(self, _btn: Gtk.Button) -> None:
        # Ocultar primero; el callback persiste el descarte en LayoutPrefs.
        self.set_visible(False)
        self._on_dismiss()
