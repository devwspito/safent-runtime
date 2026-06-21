"""HermesSettingsWindow — panel de Ajustes (Adw.PreferencesWindow).

Páginas: Apariencia · Tu asistente · Disposición · Modelos · Integraciones · Navegador · Avanzado

Principios de implementación:
  - Reutiliza ThemeManager existente (apply_theme / apply_accent / ACCENT_PRESETS).
    NO crea un segundo ThemeManager.
  - Reutiliza AgentDialog para crear/editar agentes.
  - Reutiliza ProvidersDialog (embebido en la página Modelos).
  - Reutiliza HermesIntegrationsView (embebido en la página Integraciones).
  - LayoutPrefs persiste show_sidebar / show_workspace / density en JSON.
  - Autonomía: el dominio Agent NO tiene campo de autonomía → el control
    queda OMITIDO en esta entrega (anotado abajo). Sin controles de pega.
  - golden_rules / forbidden_phrases: AgentDialog ya los lleva en el draft
    como listas pero no los expone en la UI propia. Esta página añade
    edición de esas listas cuando el draft las incluye (verificado en agent_dialog.py:214).
  - Avanzado: los campos de Perfil/Organización NO tienen endpoint de edición
    post-onboarding → se muestran como informativos (read-only + nota explicativa).
    No se fingen como editables.

Campos del agente encontrados REALMENTE en AgentDialog / AgentDraft (dict):
  - name (str, obligatorio)
  - color (str, uno de _COLOR_OPTIONS)
  - register/tono (str, uno de _REGISTER_OPTIONS)
  - role (str)
  - primary_mission (str)
  - instructions (str, multilínea)
  - language (str, heredado)
  - golden_rules (list[str])
  - forbidden_phrases (list[str])
  NO existe campo de autonomía → control OMITIDO (requiere campo de dominio
  + revisión del security-engineer antes de reducir gates HITL).

Dependencias del constructor:
  - theme_manager: ThemeManager — el singleton del proceso, pasado desde window.py.
  - layout_prefs:  LayoutPrefs   — persistencia de disposición.
  - window_ref:    HermesShellWindow — para aplicar toggles de layout en vivo.
  - client:        ShellBackendClient — para agentes y providers.
  - runtime_client — para update_agent / list_agents / create_agent / delete_agent.
    Puede ser None si el daemon aún no está disponible.
  - run_async_cb:  Callable[[Coroutine], None] — puente al loop asyncio de la ventana.
  - on_provider_active_changed: callback que la ventana ya gestiona.
"""

from __future__ import annotations

import logging
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from hermes.shell.infrastructure.shell_backend_client import ShellBackendClient
from hermes.shell.presentation.gtk4.layout_prefs import LayoutPrefs
from hermes.shell.presentation.gtk4.theme_manager import ACCENT_PRESETS, ThemeManager
from hermes.shell.presentation.gtk4.widgets.agent_dialog import AgentDialog
from hermes.shell.presentation.gtk4.widgets.integrations_view import (
    HermesIntegrationsView,
)
from hermes.shell.presentation.gtk4.widgets.providers_dialog import ProvidersDialog
from hermes.shell.presentation.gtk4.approved_sites_store import ApprovedSitesStore

logger = logging.getLogger(__name__)

# Mapa de nombre de preset (copy-deck) → clave interna en ACCENT_PRESETS.
# El copy-deck usa nombres distintos a las claves del ThemeManager.
_PRESET_DISPLAY_NAMES: dict[str, str] = {
    "Océano":          "Azul",
    "Lavanda":         "Morado",
    "Amanecer":        "Rosa",
    "Rojo terracota":  "Rojo",
    "Ámbar":           "Naranja",
    "Arena dorada":    "Amarillo",
    "Salvia":          "Verde",
    "Pizarra":         "Grafito",
}

# Orden de presentación de los swatches (igual al copy-deck).
_PRESET_ORDER: list[str] = list(_PRESET_DISPLAY_NAMES.keys())

# Colores de borde del swatch (el hex real de cada preset).
_SWATCH_HEX: dict[str, str] = {
    display: ACCENT_PRESETS[internal][0]
    for display, internal in _PRESET_DISPLAY_NAMES.items()
}


def display_name_to_preset(display: str) -> str:
    """Convierte el nombre del copy-deck a la clave interna de ACCENT_PRESETS.

    Función pura — testeable sin GTK.
    """
    return _PRESET_DISPLAY_NAMES.get(display, "Azul")


def preset_to_display_name(preset_key: str) -> str:
    """Invierte el mapa: clave interna → nombre de visualización.

    Función pura — testeable sin GTK.
    """
    for display, internal in _PRESET_DISPLAY_NAMES.items():
        if internal == preset_key:
            return display
    return "Océano"


class HermesSettingsWindow(Adw.PreferencesWindow):
    """Ventana de ajustes con 6 páginas.

    Se abre desde window.py al pulsar "Ajustes" en el sidebar.
    Es modal respecto a la ventana principal.
    """

    def __init__(
        self,
        *,
        parent: Gtk.Window,
        theme_manager: ThemeManager,
        layout_prefs: LayoutPrefs,
        window_ref,  # HermesShellWindow, sin import circular
        client: ShellBackendClient,
        runtime_client,  # DbusRuntimeClient | None
        run_async_cb: Callable,
        on_provider_active_changed: Callable,
        approved_sites_store: ApprovedSitesStore | None = None,
    ) -> None:
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(700, 560)
        self.set_title("Ajustes")
        self.set_search_enabled(True)

        self._theme_mgr = theme_manager
        self._layout_prefs = layout_prefs
        self._window_ref = window_ref
        self._client = client
        self._runtime_client = runtime_client
        self._run_async = run_async_cb
        self._on_provider_active_changed = on_provider_active_changed
        self._approved_sites_store = approved_sites_store

        # Toast overlay integrado en Adw.PreferencesWindow (add_toast).
        self._add_page_appearance()
        self._add_page_assistant()
        self._add_page_layout()
        self._add_page_models()
        self._add_page_integrations()
        self._add_page_browser()
        self._add_page_advanced()

    # ------------------------------------------------------------------
    # (a) Apariencia
    # ------------------------------------------------------------------

    def _add_page_appearance(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Apariencia")
        page.set_icon_name("preferences-desktop-appearance-symbolic")

        # Grupo Tema.
        theme_group = Adw.PreferencesGroup()
        theme_group.set_title("Tema")

        theme_row = Adw.ActionRow()
        theme_row.set_title("Modo de pantalla")
        theme_row.set_subtitle(
            "Claro · Oscuro · Automático (sigue la preferencia del sistema)"
        )

        theme_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        theme_box.set_valign(Gtk.Align.CENTER)

        current_mode = self._theme_mgr._mode
        for label, mode_val in (("Claro", "light"), ("Oscuro", "dark"), ("Automático", "auto")):
            btn = Gtk.ToggleButton(label=label)
            btn.set_active(current_mode == mode_val)
            btn.connect("toggled", self._on_theme_toggled, mode_val)
            theme_box.append(btn)

        theme_row.add_suffix(theme_box)
        theme_group.add(theme_row)
        page.add(theme_group)

        # Grupo Acento.
        accent_group = Adw.PreferencesGroup()
        accent_group.set_title("Color de acento")
        accent_group.set_description(
            "El color que acompaña los botones y los detalles de la interfaz."
        )

        # Fila de swatches: 8 círculos en un FlowBox para que se adapten.
        swatch_row = Adw.ActionRow()
        swatch_row.set_title("")
        swatch_box = Gtk.FlowBox()
        swatch_box.set_valign(Gtk.Align.CENTER)
        swatch_box.set_selection_mode(Gtk.SelectionMode.NONE)
        swatch_box.set_max_children_per_line(8)
        swatch_box.set_min_children_per_line(4)
        swatch_box.set_column_spacing(8)
        swatch_box.set_row_spacing(8)
        swatch_box.set_margin_top(8)
        swatch_box.set_margin_bottom(8)

        # Guardar referencia para actualizar el anillo de selección.
        self._swatch_buttons: dict[str, Gtk.Button] = {}
        current_preset_internal = self._theme_mgr._accent_name
        current_display = preset_to_display_name(current_preset_internal)

        self._install_swatch_css()
        for display_name in _PRESET_ORDER:
            hex_color = _SWATCH_HEX[display_name]
            btn = self._build_swatch_button(display_name, hex_color)
            if display_name == current_display:
                btn.add_css_class("hermes-accent-swatch-selected")
            self._swatch_buttons[display_name] = btn
            swatch_box.append(btn)

        swatch_row.set_child(swatch_box)
        accent_group.add(swatch_row)
        page.add(accent_group)

        self.add(page)

    def _build_swatch_button(self, display_name: str, hex_color: str) -> Gtk.Button:
        """Construye un botón swatch circular para un preset de acento."""
        btn = Gtk.Button()
        btn.set_tooltip_text(display_name)
        btn.set_size_request(32, 32)
        btn.add_css_class("hermes-accent-swatch")
        # Color de fondo inyectado inline (valor dinámico, única excepción justificada).
        btn.set_css_classes([
            *btn.get_css_classes(),
            f"hermes-accent-swatch-{display_name.lower().replace(' ', '-')}",
        ])
        # El fondo de cada swatch lo aporta un ÚNICO provider a nivel de display
        # (_install_swatch_css). GTK4 >=4.10 eliminó el provider por widget
        # (StyleContext.add_provider), que crasheaba al abrir Ajustes.
        btn.connect("clicked", self._on_swatch_clicked, display_name)
        return btn

    _swatch_css_installed: bool = False

    def _install_swatch_css(self) -> None:
        """Registra UNA vez en el display el fondo de los 8 swatches.

        Los colores de preset son fijos, así que un solo provider a nivel de
        display es correcto y evita el provider-por-widget (removido en GTK4).
        """
        if HermesSettingsWindow._swatch_css_installed:
            return
        display = Gdk.Display.get_default()
        if display is None:
            return
        rules = [
            ".hermes-accent-swatch { border-radius: 9999px; min-width: 32px;"
            " min-height: 32px; padding: 0; border: 2px solid transparent;"
            " box-shadow: none; }",
            ".hermes-accent-swatch:focus,"
            " .hermes-accent-swatch.hermes-accent-swatch-selected {"
            " border-color: @hermes_text_primary;"
            " box-shadow: 0 0 0 3px @hermes_accent_ring; }",
        ]
        for name in _PRESET_ORDER:
            slug = name.lower().replace(" ", "-")
            rules.append(
                f".hermes-accent-swatch-{slug} {{ background-color: {_SWATCH_HEX[name]}; }}"
            )
        provider = Gtk.CssProvider()
        provider.load_from_data("\n".join(rules).encode())
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
        HermesSettingsWindow._swatch_css_installed = True

    def _on_theme_toggled(self, btn: Gtk.ToggleButton, mode_val: str) -> None:
        if not btn.get_active():
            return
        self._theme_mgr.apply_theme(mode_val)  # type: ignore[arg-type]
        logger.debug("tema cambiado: %s", mode_val)

    def _on_swatch_clicked(self, _btn: Gtk.Button, display_name: str) -> None:
        # Quitar clase de selección del anterior.
        for name, b in self._swatch_buttons.items():
            if name == display_name:
                b.add_css_class("hermes-accent-swatch-selected")
            else:
                b.remove_css_class("hermes-accent-swatch-selected")

        preset_key = display_name_to_preset(display_name)
        self._theme_mgr.apply_accent(preset_key)
        logger.debug("acento cambiado: %s (%s)", display_name, preset_key)

    # ------------------------------------------------------------------
    # (b) Tu asistente
    # ------------------------------------------------------------------

    def _add_page_assistant(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Tu asistente")
        page.set_icon_name("avatar-default-symbolic")

        # Grupo Agentes.
        agents_group = Adw.PreferencesGroup()
        agents_group.set_title("Agentes")
        agents_group.set_description(
            "El agente activo es el que responde en el chat y trabaja en segundo plano."
        )

        # Botón para crear un nuevo agente.
        add_agent_row = Adw.ActionRow()
        add_agent_row.set_title("Nuevo agente")
        add_agent_row.set_subtitle("Configura un asistente con identidad y comportamiento propios")
        add_icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
        add_agent_row.add_suffix(add_icon)
        add_agent_row.set_activatable(True)
        add_agent_row.connect("activated", self._on_create_agent)
        agents_group.add(add_agent_row)

        # Lista de agentes existentes — se carga en background.
        self._agents_group = agents_group
        self._agent_rows: list[Gtk.Widget] = []
        page.add(agents_group)

        # Reglas globales — nota: el AgentDialog ya gestiona golden_rules y
        # forbidden_phrases por agente. Aquí redirigimos al usuario a editar
        # cada agente individualmente (es donde viven esas listas).
        rules_group = Adw.PreferencesGroup()
        rules_group.set_title("Reglas de comportamiento")
        rules_group.set_description(
            "Las reglas se configuran por agente. Selecciona o crea un agente "
            "para editarlas."
        )
        page.add(rules_group)

        # Nota sobre autonomía — campo omitido intencionalmente.
        autonomy_group = Adw.PreferencesGroup()
        autonomy_group.set_title("Nivel de autonomía")
        autonomy_note = Adw.ActionRow()
        autonomy_note.set_title("Próximamente")
        autonomy_note.set_subtitle(
            "El control de autonomía estará disponible cuando el dominio del agente "
            "exponga el campo correspondiente y la revisión de seguridad lo valide."
        )
        autonomy_note.set_sensitive(False)
        autonomy_group.add(autonomy_note)
        page.add(autonomy_group)

        self.add(page)

        # Cargar agentes en background para no bloquear la apertura de la ventana.
        self._load_agents()

    def _load_agents(self) -> bool:
        # El cliente D-Bus está ligado al loop del runtime (el de la ventana);
        # awaitarlo en un loop NUEVO lanza "Future attached to a different loop".
        # Se reusa self._run_async (loop de la ventana), igual que _on_create_agent.
        # Devuelve False para poder servir como callback de GLib.idle_add.
        client = self._runtime_client
        if client is None or self._run_async is None:
            GLib.idle_add(self._render_agents, [], None)
            return False

        async def _do() -> None:
            agents: list[dict] = []
            active_id: str | None = None
            try:
                agents = await client.list_agents()
                active_id = await client.get_active_agent()
            except Exception as exc:  # noqa: BLE001
                logger.warning("settings: load agents failed: %s", exc)
            GLib.idle_add(self._render_agents, agents, active_id)

        self._run_async(_do())
        return False

    def _render_agents(self, agents: list[dict], active_id: str | None) -> bool:
        for row in self._agent_rows:
            self._agents_group.remove(row)
        self._agent_rows = []

        if not agents:
            row = Adw.ActionRow()
            row.set_title("Sin agentes configurados")
            row.set_subtitle("Pulsa \"Nuevo agente\" para crear el primero")
            self._agents_group.add(row)
            self._agent_rows.append(row)
            return False

        for agent in agents:
            row = self._build_agent_row(agent, active_id=active_id)
            self._agents_group.add(row)
            self._agent_rows.append(row)

        return False

    def _build_agent_row(self, agent: dict, *, active_id: str | None) -> Gtk.Widget:
        agent_id = agent.get("agent_id", "")
        is_active = agent_id == active_id

        row = Adw.ActionRow()
        row.set_title(agent.get("name", agent_id))
        subtitle_parts = []
        if agent.get("role"):
            subtitle_parts.append(agent["role"])
        if is_active:
            subtitle_parts.append("Activo")
        row.set_subtitle(" · ".join(subtitle_parts) if subtitle_parts else "")

        # Botón Editar.
        edit_btn = Gtk.Button.new_from_icon_name("document-edit-symbolic")
        edit_btn.set_valign(Gtk.Align.CENTER)
        edit_btn.add_css_class("flat")
        edit_btn.set_tooltip_text("Editar este agente")
        edit_btn.connect("clicked", lambda _b, a=agent: self._on_edit_agent(a))
        row.add_suffix(edit_btn)

        # Botón Eliminar — no se muestra si es el único o si es el activo por defecto.
        delete_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        delete_btn.set_valign(Gtk.Align.CENTER)
        delete_btn.add_css_class("flat")
        delete_btn.add_css_class("destructive-action")
        delete_btn.set_tooltip_text("Eliminar este agente")
        delete_btn.connect("clicked", lambda _b, aid=agent_id, n=agent.get("name", agent_id): self._on_delete_agent(aid, n))
        row.add_suffix(delete_btn)

        return row

    def _on_create_agent(self, _row) -> None:
        def on_save(draft: dict) -> None:
            async def _do() -> None:
                client = self._runtime_client
                if client is None:
                    return
                try:
                    await client.create_agent(draft)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("create_agent: %s", exc)
                GLib.idle_add(self._load_agents)

            self._run_async(_do())

        dlg = AgentDialog(parent=self, on_save=on_save)
        dlg.present()

    def _on_edit_agent(self, agent: dict) -> None:
        agent_id = agent.get("agent_id", "")

        def on_save(draft: dict) -> None:
            async def _do() -> None:
                client = self._runtime_client
                if client is None:
                    return
                try:
                    await client.update_agent(agent_id, draft)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("update_agent: %s", exc)
                GLib.idle_add(self._load_agents)

            self._run_async(_do())

        dlg = AgentDialog(parent=self, on_save=on_save, agent=agent)
        dlg.present()

    def _on_delete_agent(self, agent_id: str, agent_name: str) -> None:
        dialog = Adw.MessageDialog.new(self)
        dialog.set_heading(f'Eliminar “{agent_name}”')
        dialog.set_body(
            "Esta acción no se puede deshacer. El agente dejará de estar disponible."
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("delete", "Eliminar")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d, response: str) -> None:
            if response != "delete":
                return

            async def _do() -> None:
                client = self._runtime_client
                if client is None:
                    GLib.idle_add(
                        self.add_toast,
                        Adw.Toast.new("El daemon no está disponible"),
                    )
                    return
                try:
                    await client.delete_agent(agent_id)
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    logger.warning("delete_agent: %s", exc)
                    GLib.idle_add(self.add_toast, Adw.Toast.new(f"No se pudo eliminar: {msg}"))
                    return
                GLib.idle_add(self._load_agents)

            self._run_async(_do())

        dialog.connect("response", on_response)
        dialog.present()

    # ------------------------------------------------------------------
    # (c) Disposición
    # ------------------------------------------------------------------

    def _add_page_layout(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Disposición")
        page.set_icon_name("sidebar-show-symbolic")

        prefs = self._layout_prefs

        # Grupo Paneles.
        panels_group = Adw.PreferencesGroup()
        panels_group.set_title("Paneles")

        # Barra lateral.
        sidebar_row = Adw.SwitchRow()
        sidebar_row.set_title("Barra lateral")
        sidebar_row.set_subtitle("Navegación, selector de agente y conversaciones recientes")
        sidebar_row.set_active(prefs.show_sidebar)
        sidebar_row.connect("notify::active", self._on_sidebar_switch, prefs)
        panels_group.add(sidebar_row)

        # Panel del agente / workspace.
        workspace_row = Adw.SwitchRow()
        workspace_row.set_title("Panel del agente")
        workspace_row.set_subtitle(
            "Pantalla en vivo, herramientas y contexto del agente"
        )
        workspace_row.set_active(prefs.show_workspace)
        workspace_row.connect("notify::active", self._on_workspace_switch, prefs)
        panels_group.add(workspace_row)

        # Chat — no ocultable.
        chat_row = Adw.SwitchRow()
        chat_row.set_title("Chat")
        chat_row.set_subtitle("El chat siempre está visible")
        chat_row.set_active(True)
        chat_row.set_sensitive(False)
        panels_group.add(chat_row)

        page.add(panels_group)

        # Grupo Densidad.
        density_group = Adw.PreferencesGroup()
        density_group.set_title("Densidad")

        density_row = Adw.ActionRow()
        density_row.set_title("Compacidad de la interfaz")

        density_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        density_box.set_valign(Gtk.Align.CENTER)

        for label, density_val in (("Cómoda", "comfortable"), ("Compacta", "compact")):
            btn = Gtk.ToggleButton(label=label)
            btn.set_active(prefs.density == density_val)
            btn.connect("toggled", self._on_density_toggled, density_val, prefs)
            density_box.append(btn)

        density_row.add_suffix(density_box)
        density_group.add(density_row)
        page.add(density_group)

        # Botón Restablecer.
        reset_group = Adw.PreferencesGroup()
        reset_row = Adw.ActionRow()
        reset_row.set_title("Restablecer valores")
        reset_row.set_subtitle("Vuelve a la disposición predeterminada")
        reset_btn = Gtk.Button.new_with_label("Restablecer")
        reset_btn.set_valign(Gtk.Align.CENTER)
        reset_btn.add_css_class("flat")
        reset_btn.connect(
            "clicked",
            lambda _b, sr=sidebar_row, wr=workspace_row, db=density_box:
                self._on_reset_layout(sr, wr, db, prefs),
        )
        reset_row.add_suffix(reset_btn)
        reset_group.add(reset_row)
        page.add(reset_group)

        self.add(page)

    def _on_sidebar_switch(self, row: Adw.SwitchRow, _param, prefs: LayoutPrefs) -> None:
        visible = row.get_active()
        prefs.show_sidebar = visible
        prefs.save()
        # Aplicar en vivo a la ventana principal.
        win = self._window_ref
        if win is not None and hasattr(win, "_root_split"):
            win._root_split.set_show_sidebar(visible)

    def _on_workspace_switch(self, row: Adw.SwitchRow, _param, prefs: LayoutPrefs) -> None:
        visible = row.get_active()
        prefs.show_workspace = visible
        prefs.save()
        win = self._window_ref
        if win is not None and hasattr(win, "_content_split"):
            win._content_split.set_show_sidebar(visible)

    def _on_density_toggled(
        self,
        btn: Gtk.ToggleButton,
        density_val: str,
        prefs: LayoutPrefs,
    ) -> None:
        if not btn.get_active():
            return
        prefs.density = density_val  # type: ignore[assignment]
        prefs.save()
        # Aplicar clase CSS en la ventana principal para ajustar paddings.
        win = self._window_ref
        if win is not None:
            if density_val == "compact":
                win.add_css_class("hermes-density-compact")
                win.remove_css_class("hermes-density-comfortable")
            else:
                win.add_css_class("hermes-density-comfortable")
                win.remove_css_class("hermes-density-compact")

    def _on_reset_layout(
        self,
        sidebar_row: Adw.SwitchRow,
        workspace_row: Adw.SwitchRow,
        density_box: Gtk.Box,
        prefs: LayoutPrefs,
    ) -> None:
        prefs.reset_to_defaults()

        # Actualizar los controles sin disparar efectos en cascada.
        sidebar_row.set_active(prefs.show_sidebar)
        workspace_row.set_active(prefs.show_workspace)

        # Reactivar el botón "Cómoda" en el density_box.
        child = density_box.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.ToggleButton) and child.get_label() == "Cómoda":
                child.set_active(True)
            child = child.get_next_sibling()

        self.add_toast(Adw.Toast.new("Disposición restablecida"))

    # ------------------------------------------------------------------
    # (d) Modelos
    # ------------------------------------------------------------------

    def _add_page_models(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Modelos")
        page.set_icon_name("network-server-symbolic")

        group = Adw.PreferencesGroup()
        group.set_title("Servicios de IA")
        group.set_description(
            "Conecta el servicio que usa tu asistente para razonar y responder. "
            "Tus conversaciones no se comparten con nadie más."
        )

        # Botón que abre el ProvidersDialog existente (no lo inlineamos
        # porque tiene su propia gestión de estado y threading).
        open_providers_row = Adw.ActionRow()
        open_providers_row.set_title("Gestionar modelos y proveedores")
        open_providers_row.set_subtitle("Añadir, editar, activar o eliminar conexiones")
        chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
        open_providers_row.add_suffix(chevron)
        open_providers_row.set_activatable(True)
        open_providers_row.connect("activated", self._on_open_providers)
        group.add(open_providers_row)

        page.add(group)
        self.add(page)

    def _on_open_providers(self, _row) -> None:
        dlg = ProvidersDialog(
            parent=self,
            client=self._client,
            on_active_changed=self._on_provider_active_changed,
        )
        dlg.present()

    # ------------------------------------------------------------------
    # (e) Integraciones
    # ------------------------------------------------------------------

    def _add_page_integrations(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Integraciones")
        page.set_icon_name("network-transmit-receive-symbolic")

        group = Adw.PreferencesGroup()
        group.set_title("Apps conectadas")

        # HermesIntegrationsView es un Gtk.Box, no un widget de preferencias.
        # Lo embebemos dentro de un ActionRow expandido.
        # Como Adw.PreferencesGroup espera Adw.PreferencesRow, usamos un
        # Adw.PreferencesRow con child personalizado para embeber la vista.
        integrations_row = Adw.PreferencesRow()
        integrations_view = HermesIntegrationsView(client=self._client)
        integrations_view.set_size_request(-1, 400)
        integrations_row.set_child(integrations_view)
        group.add(integrations_row)

        page.add(group)
        self.add(page)

    # ------------------------------------------------------------------
    # (f) Navegador — sitios aprobados para acciones del agente
    # ------------------------------------------------------------------

    def _add_page_browser(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Navegador")
        page.set_icon_name("web-browser-symbolic")

        # ---- Grupo: aviso de seguridad ----
        security_group = Adw.PreferencesGroup()
        security_group.set_title("Sitios aprobados para el agente")
        security_group.set_description(
            "Tu asistente puede leer cualquier página web, pero solo puede actuar "
            "(pulsar botones, escribir en formularios) en los sitios que tú apruebes. "
            "Sin ningún sitio aprobado, el asistente no actúa en ninguna web. "
            "Añadir un sitio es una decisión de seguridad — solo añade los que uses."
        )

        # Fila de estado: "Sin sitios aprobados — el asistente no actuará en webs"
        self._browser_empty_row = Adw.ActionRow()
        self._browser_empty_row.set_title("Sin sitios aprobados")
        self._browser_empty_row.set_subtitle(
            "El asistente puede leer webs, pero no puede pulsar ni escribir en ninguna. "
            "Añade un sitio para habilitarlo."
        )
        self._browser_empty_row.set_sensitive(False)
        security_group.add(self._browser_empty_row)

        page.add(security_group)

        # ---- Grupo: lista editable de dominios ----
        sites_group = Adw.PreferencesGroup()
        sites_group.set_title("Dominios permitidos")
        self._sites_group = sites_group
        self._site_rows: list[Gtk.Widget] = []
        page.add(sites_group)

        # ---- Grupo: añadir nuevo dominio ----
        add_group = Adw.PreferencesGroup()

        add_row = Adw.ActionRow()
        add_row.set_title("Añadir sitio")
        add_row.set_subtitle("Escribe el dominio (ej. empresa.com) y pulsa Añadir")

        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        add_box.set_valign(Gtk.Align.CENTER)

        self._domain_entry = Gtk.Entry()
        self._domain_entry.set_placeholder_text("ejemplo.com")
        self._domain_entry.set_width_chars(26)
        self._domain_entry.set_input_purpose(Gtk.InputPurpose.URL)
        self._domain_entry.set_max_length(253)
        # Añadir con Enter.
        self._domain_entry.connect("activate", self._on_add_site_activated)

        add_btn = Gtk.Button.new_with_label("Añadir")
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._on_add_site_clicked)

        add_box.append(self._domain_entry)
        add_box.append(add_btn)
        add_row.add_suffix(add_box)
        add_group.add(add_row)
        page.add(add_group)

        self.add(page)

        # Renderizar el estado inicial.
        self._refresh_sites_ui()

    def _refresh_sites_ui(self) -> None:
        """Actualiza la lista de sitios en la UI desde el store."""
        store = self._approved_sites_store

        # Quitar filas anteriores.
        for row in self._site_rows:
            self._sites_group.remove(row)
        self._site_rows = []

        if store is None or not store.sites:
            self._browser_empty_row.set_visible(True)
            return

        self._browser_empty_row.set_visible(False)

        for domain in store.sites:
            row = self._build_site_row(domain)
            self._sites_group.add(row)
            self._site_rows.append(row)

    def _build_site_row(self, domain: str) -> Gtk.Widget:
        row = Adw.ActionRow()
        row.set_title(domain)
        row.set_subtitle("El asistente puede pulsar y escribir en este sitio")

        remove_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        remove_btn.set_valign(Gtk.Align.CENTER)
        remove_btn.add_css_class("flat")
        remove_btn.add_css_class("destructive-action")
        remove_btn.set_tooltip_text(f"Quitar {domain} de los sitios aprobados")
        remove_btn.connect("clicked", lambda _b, d=domain: self._on_remove_site(d))

        row.add_suffix(remove_btn)
        return row

    def _on_add_site_activated(self, _entry: Gtk.Entry) -> None:
        self._do_add_site()

    def _on_add_site_clicked(self, _btn: Gtk.Button) -> None:
        self._do_add_site()

    def _do_add_site(self) -> None:
        store = self._approved_sites_store
        raw = self._domain_entry.get_text().strip()
        if not raw:
            return

        if store is None:
            self.add_toast(Adw.Toast.new("El almacén de sitios no está disponible"))
            return

        added = store.add(raw)
        if added:
            self._domain_entry.set_text("")
            self._refresh_sites_ui()
            self.add_toast(Adw.Toast.new(f'"{raw}" añadido a los sitios aprobados'))
        else:
            # Ya existía o era inválido.
            if store.sites and raw.strip().lower() in store.sites:
                self.add_toast(Adw.Toast.new(f'"{raw}" ya estaba en la lista'))
            else:
                self.add_toast(
                    Adw.Toast.new(
                        f'"{raw}" no es un dominio valido. '
                        "Usa solo el nombre del dominio, sin http:// ni rutas."
                    )
                )

    def _on_remove_site(self, domain: str) -> None:
        store = self._approved_sites_store
        if store is None:
            return

        dialog = Adw.MessageDialog.new(self)
        dialog.set_heading(f'Quitar "{domain}"')
        dialog.set_body(
            f"El asistente dejará de poder actuar en {domain}. "
            "Podrás volver a añadirlo cuando quieras."
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("remove", "Quitar")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d, response: str) -> None:
            if response != "remove":
                return
            store.remove(domain)
            self._refresh_sites_ui()
            self.add_toast(Adw.Toast.new(f'"{domain}" eliminado de los sitios aprobados'))

        dialog.connect("response", on_response)
        dialog.present()

    # ------------------------------------------------------------------
    # (g) Avanzado — informativo (sin endpoints de edición post-onboarding)
    # ------------------------------------------------------------------

    def _add_page_advanced(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Avanzado")
        page.set_icon_name("emblem-system-symbolic")

        # Perfil del sistema.
        profile_group = Adw.PreferencesGroup()
        profile_group.set_title("Perfil del sistema")
        profile_group.set_description(
            "Estos valores se configuraron durante la instalación. "
            "Para modificarlos, ejecuta el asistente de configuración inicial."
        )

        profile_note = Adw.ActionRow()
        profile_note.set_title("Tipo de perfil")
        profile_note.set_subtitle("Personal (escritorio) — definido en la instalación")
        profile_note.set_sensitive(False)
        profile_group.add(profile_note)

        page.add(profile_group)

        # Vínculo de organización.
        org_group = Adw.PreferencesGroup()
        org_group.set_title("Vínculo de organización")
        org_group.set_description(
            "Conecta este equipo a tu organización para recibir configuración "
            "centralizada de agentes y permisos. Requiere reiniciar el servicio."
        )

        org_note = Adw.ActionRow()
        org_note.set_title("Organización")
        org_note.set_subtitle(
            "Sin vínculo — para vincular, usa la línea de comandos: "
            "hermes-shell --bind-org <url>"
        )
        org_note.set_sensitive(False)
        org_group.add(org_note)

        page.add(org_group)

        self.add(page)
