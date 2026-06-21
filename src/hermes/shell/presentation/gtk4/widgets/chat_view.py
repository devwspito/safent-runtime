"""HermesChatView — stream de mensajes user/agent + markdown + tool cards.

Novedades US3 (spec 011):
  - Empty state moderno: saludo + 4 chips clicables (→ composer).
  - Typing indicator (3 dots) mientras AWAITING_FIRST_TOKEN.
  - Caret ▌ parpadeante al final del texto durante streaming (GLib.timeout_add
    600ms); desaparece exactamente en finalize_streaming_bubble.
  - append_action_card() — ChatActionCard inline (sin-modelo, error, aprobación).
  - Icono symbolic en tool cards (applications-utilities-symbolic).
  - set_no_model_mode() para mostrar el empty state sin-modelo.
  - Contrato público: start_streaming_agent_message / append_delta_to_streaming_bubble
    / finalize_streaming_bubble / append_user_message / append_tool_call preservados.

El caller (window.py) controla:
  - cuándo mostrar el typing indicator (llamando a start_streaming_agent_message
    en estado AWAITING_FIRST_TOKEN antes de recibir el primer delta).
  - cuándo hacer crossfade a texto real (append_delta_to_streaming_bubble convierte
    automáticamente el typing indicator en texto).
  - cuándo el caret desaparece (finalize_streaming_bubble).

Animaciones:
  - prefers-reduced-motion respetado: los timers de animación no registran
    timeouts; el caret queda fijo (▌ estático) y los dots quedan estáticos.
  - Los dots se animan con GLib.timeout_add (400ms loop).
  - El caret parpadea con GLib.timeout_add (600ms); cada tick alterna el
    carácter visible/oculto sobre _progress_label.
  - Los source ids se guardan como atributos Python en los containers
    (PyGObject moderno eliminó set_data/get_data); se cancelan en finalize.
"""

from __future__ import annotations

import logging
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from hermes.shell.presentation.gtk4.widgets.markdown_render import (
    render_markdown_to_pango,
)

logger = logging.getLogger(__name__)

# Chips del empty state con modelo (copy-deck §Chat).
_EXAMPLE_CHIPS = [
    "Redacta un correo formal por mí",
    "Resume este documento",
    "Explícame algo paso a paso",
    "Ayúdame a organizar mi semana",
]

# Los 3 frames de los typing dots (escalonados, loop continuo).
_DOT_FRAMES = ["● ○ ○", "○ ● ○", "○ ○ ●"]


def _prefers_reduced_motion() -> bool:
    """Devuelve True si el sistema tiene activada la preferencia de movimiento reducido."""
    try:
        settings = Gtk.Settings.get_default()
        if settings is None:
            return False
        return bool(settings.get_property("gtk-enable-animations") is False)
    except Exception:  # noqa: BLE001
        return False


class HermesChatView(Gtk.Box):
    """Lista vertical de mensajes con markdown render y animaciones de stream."""

    def __init__(
        self,
        on_chip_clicked: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_css_class("hermes-chat-stream")
        self.set_hexpand(True)

        # Callback para cuando el usuario hace clic en un chip del empty state.
        # window.py lo conecta para rellenar el composer.
        self._on_chip_clicked = on_chip_clicked

        # Typing indicator — se crea una vez y se reutiliza.
        self._typing_timeout_id: int | None = None
        self._dot_frame_index: int = 0

        self._show_empty_state(has_model=True)

    # ------------------------------------------------------------------
    # Empty state
    # ------------------------------------------------------------------

    def _show_empty_state(self, has_model: bool = True) -> None:
        """Construye y añade el empty state al inicio."""
        if has_model:
            self._empty_state = self._build_empty_with_model()
        else:
            self._empty_state = self._build_empty_no_model()
        self.append(self._empty_state)

    def _build_empty_with_model(self) -> Gtk.Box:
        """Saludo + 4 chips clicables (copy-deck §Chat/Empty con modelo)."""
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        root.add_css_class("hermes-chat-empty-modern")
        root.set_hexpand(True)
        root.set_vexpand(True)
        root.set_valign(Gtk.Align.CENTER)
        root.set_halign(Gtk.Align.CENTER)

        # El nombre del asistente se resuelve en ventana; si no se inicia con
        # nombre, se usa "tu asistente" como fallback seguro.
        greeting = Gtk.Label(label="Hola, soy tu asistente. ¿En qué puedo ayudarte hoy?")
        greeting.add_css_class("hermes-chat-greeting")
        greeting.set_wrap(True)
        greeting.set_xalign(0.5)
        root.append(greeting)

        # FlowBox (no Gtk.Box) para que los chips se envuelvan a 2 por línea:
        # Gtk.Box no tiene set_wrap y 4 chips en horizontal desbordarían el clamp.
        chips_row = Gtk.FlowBox()
        chips_row.set_selection_mode(Gtk.SelectionMode.NONE)
        chips_row.set_halign(Gtk.Align.CENTER)
        chips_row.set_max_children_per_line(2)
        chips_row.set_min_children_per_line(1)
        chips_row.set_column_spacing(8)
        chips_row.set_row_spacing(8)
        for chip_text in _EXAMPLE_CHIPS:
            btn = Gtk.Button(label=chip_text)
            btn.add_css_class("hermes-chat-chip")
            # Closure sobre chip_text.
            btn.connect("clicked", self._make_chip_handler(chip_text))
            chips_row.append(btn)
        root.append(chips_row)

        return root

    def _build_empty_no_model(self) -> Gtk.Box:
        """Empty state sin modelo (copy-deck §Chat/Empty sin modelo)."""
        page = Adw.StatusPage()
        page.set_icon_name("emblem-system-symbolic")
        page.set_title("Tu asistente todavía no puede responder")
        page.set_description(
            "Conéctale un servicio para que empiece a trabajar contigo. "
            "Solo necesitas unos datos del servicio que elijas."
        )
        connect_btn = Gtk.Button(label="Conectar ahora")
        connect_btn.add_css_class("hermes-primary")
        connect_btn.connect("clicked", lambda _b: self._on_chip_clicked("__connect_model__") if self._on_chip_clicked else None)
        page.set_child(connect_btn)

        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrapper.set_hexpand(True)
        wrapper.set_vexpand(True)
        wrapper.set_valign(Gtk.Align.CENTER)
        wrapper.append(page)
        return wrapper

    def _make_chip_handler(self, text: str) -> Callable:
        def _handler(_btn) -> None:
            if self._on_chip_clicked is not None:
                self._on_chip_clicked(text)
        return _handler

    def set_greeting(self, assistant_name: str) -> None:
        """Actualiza el saludo del empty state con el nombre real del asistente.

        Llamado desde window.py cuando se conoce el nombre del agente activo.
        Solo tiene efecto si el empty state sigue visible (sin mensajes).
        """
        if self._empty_state is None:
            return
        # Buscar el label de saludo dentro del empty state.
        child = self._empty_state.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label) and child.has_css_class("hermes-chat-greeting"):
                child.set_label(
                    f"Hola, soy {assistant_name}. ¿En qué puedo ayudarte hoy?"
                )
                return
            child = child.get_next_sibling()

    def set_no_model_mode(self, no_model: bool) -> None:
        """Alterna entre el empty state con/sin modelo.

        Solo tiene efecto si el chat está vacío (empty state visible).
        """
        if self._empty_state is None:
            return
        self.remove(self._empty_state)
        self._empty_state = None  # type: ignore[assignment]
        self._show_empty_state(has_model=not no_model)

    def _ensure_no_empty(self) -> None:
        if getattr(self, "_empty_state", None) is not None:
            self.remove(self._empty_state)
            self._empty_state = None  # type: ignore[assignment]

    def clear(self) -> None:
        """Vacía toda la conversación visible (al cambiar de chat)."""
        self._stop_typing_animation()
        while (child := self.get_first_child()) is not None:
            self.remove(child)
        self._show_empty_state(has_model=True)

    # ------------------------------------------------------------------
    # Mensajes
    # ------------------------------------------------------------------

    def append_user_message(self, text: str) -> Gtk.Label:
        """Pinta el bubble de usuario. Devuelve el widget (para tests/referencia)."""
        self._ensure_no_empty()
        bubble = Gtk.Label(label=text)
        bubble.set_wrap(True)
        bubble.set_xalign(0)
        bubble.set_halign(Gtk.Align.END)
        bubble.set_max_width_chars(60)
        bubble.add_css_class("hermes-chat-message-user")
        bubble.set_selectable(True)
        self.append(bubble)
        return bubble

    def append_agent_message(self, text: str) -> None:
        """Render markdown del agent message (sin burbuja, texto plano)."""
        self._ensure_no_empty()
        container = self._build_agent_container()
        self._render_markdown_into(container, text)
        self.append(container)

    def append_agent_unavailable(self) -> None:
        """Mensaje no técnico cuando el daemon no está disponible."""
        self.append_agent_message(
            "El agente no está disponible en este momento. "
            "Se reconectará automáticamente cuando el servicio esté listo."
        )

    def append_action_card(
        self,
        *,
        message: str,
        button_label: str,
        on_action: Callable[[], None],
    ) -> Gtk.Box:
        """ChatActionCard reutilizable: copy + 1 botón CTA.

        Usos: sin-modelo, sin-Composio, error de turno (Reintentar),
        aprobación pendiente (Aprobar), etc.

        Devuelve la card para que el caller pueda referenciarla
        (p.ej. para quitarla al reintentar).
        """
        self._ensure_no_empty()
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("hermes-action-card")
        card.set_halign(Gtk.Align.START)

        msg_label = Gtk.Label(label=message)
        msg_label.set_wrap(True)
        msg_label.set_xalign(0)
        msg_label.add_css_class("hermes-action-card-message")
        card.append(msg_label)

        btn = Gtk.Button(label=button_label)
        btn.add_css_class("hermes-ghost")
        btn.set_halign(Gtk.Align.START)
        btn.connect("clicked", lambda _b: on_action())
        card.append(btn)

        self.append(card)
        return card

    def remove_widget(self, widget: Gtk.Widget) -> None:
        """Elimina un widget hijo (usado para quitar action cards al reintentar)."""
        try:
            self.remove(widget)
        except Exception:  # noqa: BLE001
            pass

    def _build_agent_container(self) -> Gtk.Box:
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        container.add_css_class("hermes-chat-message-agent")
        container.set_halign(Gtk.Align.START)
        return container

    def _render_markdown_into(
        self, container: Gtk.Box, text: str
    ) -> None:
        """Limpia container y vuelve a renderizar markdown."""
        while (child := container.get_first_child()) is not None:
            container.remove(child)
        for block in render_markdown_to_pango(text):
            if block.kind == "code":
                self._append_code_block(container, block.content, block.language)
            else:
                label = Gtk.Label()
                try:
                    label.set_markup(block.content)
                except Exception:  # noqa: BLE001
                    label.set_text(block.content)
                label.set_wrap(True)
                label.set_xalign(0)
                label.set_halign(Gtk.Align.START)
                label.set_max_width_chars(80)
                label.set_selectable(True)
                container.append(label)

    def _append_code_block(
        self, container: Gtk.Box, code: str, language: str | None
    ) -> None:
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        wrap.add_css_class("hermes-tool-card")
        if language:
            header = Gtk.Label(label=language)
            header.add_css_class("hermes-tool-card-header")
            header.set_xalign(0)
            wrap.append(header)
        body = Gtk.Label(label=code)
        body.set_xalign(0)
        body.set_wrap(True)
        body.set_selectable(True)
        body.add_css_class("hermes-code-block")
        wrap.append(body)
        container.append(wrap)

    # ------------------------------------------------------------------
    # Streaming — contrato público preservado + typing + caret
    # ------------------------------------------------------------------

    def start_streaming_agent_message(self) -> Gtk.Box:
        """Crea un bubble agent con typing indicator.

        El caller guarda la referencia al container devuelto y la pasa a
        append_delta_to_streaming_bubble / finalize_streaming_bubble.

        Estado interno almacenado en atributos Python del container
        (PyGObject moderno eliminó set_data/get_data).

        Typing dots:
          - Se muestran hasta que llega el primer delta.
          - Se animan con GLib.timeout_add salvo prefers-reduced-motion.
          - El crossfade a texto real ocurre dentro de
            append_delta_to_streaming_bubble en la primera llamada.
        """
        self._ensure_no_empty()
        container = self._build_agent_container()

        # Acumulador de texto del stream.
        container._text_acc = []
        # True mientras el primer delta no ha llegado.
        container._is_typing = True
        # True mientras el stream está activo (para el caret).
        container._is_streaming = True
        # Source id del timer del caret; None = sin timer activo.
        # Se inicializa aquí para que _stop_caret_animation sea seguro
        # incluso cuando el stream se cancela antes del primer delta.
        container._caret_timeout_id = None

        # Label de typing dots (se reemplaza con el label de progreso en
        # el primer delta).
        typing_label = Gtk.Label(label=_DOT_FRAMES[0])
        typing_label.add_css_class("hermes-typing-dots")
        typing_label.set_xalign(0)
        typing_label.set_halign(Gtk.Align.START)
        container.append(typing_label)
        container._typing_label = typing_label

        # Label de progreso (inicialmente oculto, se muestra en el primer delta).
        progress_label = Gtk.Label(label="")
        progress_label.set_wrap(True)
        progress_label.set_xalign(0)
        progress_label.set_halign(Gtk.Align.START)
        progress_label.set_max_width_chars(80)
        progress_label.set_selectable(True)
        progress_label.set_visible(False)
        container.append(progress_label)
        container._progress_label = progress_label

        self.append(container)

        # Arrancar animación de typing dots.
        self._start_typing_animation(container)

        return container

    def _start_typing_animation(self, container: Gtk.Box) -> None:
        """Registra el timeout de animación de los typing dots.

        Si prefers-reduced-motion está activado, los dots quedan estáticos.
        """
        if _prefers_reduced_motion():
            return

        self._stop_typing_animation()

        def _tick() -> bool:
            if not getattr(container, "_is_typing", False):
                self._typing_timeout_id = None
                return False  # GLib: no repetir
            self._dot_frame_index = (self._dot_frame_index + 1) % len(_DOT_FRAMES)
            typing_label = getattr(container, "_typing_label", None)
            if typing_label is not None:
                typing_label.set_label(_DOT_FRAMES[self._dot_frame_index])
            return True  # GLib: repetir

        self._typing_timeout_id = GLib.timeout_add(400, _tick)

    def _stop_typing_animation(self) -> None:
        if self._typing_timeout_id is not None:
            GLib.source_remove(self._typing_timeout_id)
            self._typing_timeout_id = None

    def append_delta_to_streaming_bubble(
        self, container: Gtk.Box, delta: str
    ) -> None:
        """Añade delta al stream.

        Primera llamada: elimina los typing dots, muestra el label de progreso
        y arranca el timer de parpadeo del caret.
        Llamadas siguientes: acumula texto; el timer pinta el caret de forma
        independiente (no se duplica el caret en el texto base).
        """
        acc = getattr(container, "_text_acc", None) or []
        acc.append(delta)
        container._text_acc = acc

        # Primera vez: crossfade del typing indicator al texto + arranque del caret.
        if getattr(container, "_is_typing", False):
            container._is_typing = False
            self._stop_typing_animation()
            typing_label = getattr(container, "_typing_label", None)
            if typing_label is not None:
                container.remove(typing_label)
                container._typing_label = None  # type: ignore[assignment]
            progress_label = getattr(container, "_progress_label", None)
            if progress_label is not None:
                progress_label.set_visible(True)
            self._start_caret_animation(container)

        full = "".join(acc)
        progress = getattr(container, "_progress_label", None)
        if progress is not None:
            # El timer de caret pinta ▌ o espacio; aquí pintamos el texto base
            # con el caret visible para que el primer render no tarde al tick.
            progress.set_text(full + " ▌")

    def _start_caret_animation(self, container: Gtk.Box) -> None:
        """Registra el timeout de parpadeo del caret (600ms).

        Si prefers-reduced-motion, el caret queda fijo (▌ estático, sin timer).
        El source id se guarda en container._caret_timeout_id para cancelarlo
        en finalize_streaming_bubble — nunca deja timers huérfanos.
        """
        if _prefers_reduced_motion():
            # Caret fijo: ya pintado en el primer set_text de append_delta.
            return

        caret_visible = True

        def _tick() -> bool:
            nonlocal caret_visible
            if not getattr(container, "_is_streaming", False):
                container._caret_timeout_id = None  # type: ignore[assignment]
                return False  # GLib: no repetir
            acc = getattr(container, "_text_acc", None) or []
            full = "".join(acc)
            progress = getattr(container, "_progress_label", None)
            if progress is None:
                container._caret_timeout_id = None  # type: ignore[assignment]
                return False
            # Alternar entre caret visible e invisible.
            # Dos espacios de igual ancho que ▌ para evitar saltos de layout.
            caret_visible = not caret_visible
            suffix = " ▌" if caret_visible else "  "
            progress.set_text(full + suffix)
            return True  # GLib: repetir

        container._caret_timeout_id = GLib.timeout_add(600, _tick)

    def _stop_caret_animation(self, container: Gtk.Box) -> None:
        """Cancela el timer del caret si existe. Idempotente."""
        timeout_id = getattr(container, "_caret_timeout_id", None)
        if timeout_id is not None:
            GLib.source_remove(timeout_id)
            container._caret_timeout_id = None  # type: ignore[assignment]

    def finalize_streaming_bubble(self, container: Gtk.Box) -> None:
        """Llamar en `done`: reemplaza el progress label por markdown render.

        Cancela el timer del caret, detiene typing, renderiza markdown.
        Añade las acciones Regenerar + Copiar bajo el bubble.
        """
        # Marcar streaming terminado antes de cancelar el timer para que
        # cualquier tick pendiente vea _is_streaming=False y salga limpio.
        container._is_typing = False
        container._is_streaming = False
        self._stop_typing_animation()
        self._stop_caret_animation(container)

        acc = getattr(container, "_text_acc", None) or []
        full = "".join(acc)
        self._render_markdown_into(container, full)
        container._text_acc = None
        container._progress_label = None

        # Añadir fila de acciones (Regenerar + Copiar) bajo el bubble.
        self._append_agent_actions(container, text=full)

    def _append_agent_actions(self, container: Gtk.Box, text: str) -> None:
        """Fila discreta Regenerar ↻ + Copiar bajo el último bubble del agente.

        Visible al hover mediante CSS (hermes-agent-actions / :hover).
        El botón Regenerar emite la señal de chip especial __regenerate__.
        """
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        actions.add_css_class("hermes-agent-actions")

        copy_btn = Gtk.Button()
        copy_btn.add_css_class("flat")
        copy_btn.add_css_class("hermes-agent-action-btn")
        copy_icon = Gtk.Image.new_from_icon_name("edit-copy-symbolic")
        copy_btn.set_child(copy_icon)
        copy_btn.set_tooltip_text("Copiar")
        copy_btn.connect("clicked", lambda _b: self._copy_to_clipboard(text))
        actions.append(copy_btn)

        regen_btn = Gtk.Button()
        regen_btn.add_css_class("flat")
        regen_btn.add_css_class("hermes-agent-action-btn")
        regen_icon = Gtk.Image.new_from_icon_name("view-refresh-symbolic")
        regen_btn.set_child(regen_icon)
        regen_btn.set_tooltip_text("Regenerar respuesta")
        regen_btn.connect(
            "clicked",
            lambda _b: self._on_chip_clicked("__regenerate__") if self._on_chip_clicked else None,
        )
        actions.append(regen_btn)

        container.append(actions)

    def _copy_to_clipboard(self, text: str) -> None:
        """Copia text al portapapeles del sistema."""
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                clipboard = display.get_clipboard()
                clipboard.set(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("clipboard copy failed: %s", exc)

    # ------------------------------------------------------------------
    # Tool call cards — icono symbolic en vez de emoji
    # ------------------------------------------------------------------

    def append_tool_call(self, *, tool_name: str, payload_preview: str) -> None:
        """Tarjeta de tool call con icono symbolic accent (no emoji 🔧)."""
        self._ensure_no_empty()
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("hermes-tool-card")

        # Header: icono symbolic + nombre (caption-strong accent).
        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tool_icon = Gtk.Image.new_from_icon_name("applications-utilities-symbolic")
        tool_icon.add_css_class("hermes-tool-card-icon")
        header_row.append(tool_icon)

        header_label = Gtk.Label(label=tool_name)
        header_label.add_css_class("hermes-tool-card-header")
        header_label.set_xalign(0)
        header_row.append(header_label)
        card.append(header_row)

        # Body: mono 13, truncado.
        preview = payload_preview[:200] if payload_preview else ""
        body = Gtk.Label(label=preview)
        body.set_xalign(0)
        body.set_wrap(True)
        body.set_selectable(True)
        body.add_css_class("hermes-tool-card-body")
        card.append(body)

        self.append(card)
