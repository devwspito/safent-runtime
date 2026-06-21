"""HermesShellWindow — la ventana principal del SO.

Layout 3-pane usando dos AdwOverlaySplitView anidados:
  root_split:    sidebar izquierda | content_split
  content_split: center (chat)     | workspace (trailing)

El contenido del chat y el composer viven dentro de Adw.Clamp(720)
para fijar el ancho de lectura independientemente del ancho del panel.

Breakpoints responsivos (Adw.Breakpoint):
  <720px:  sidebar izquierda colapsa a overlay.
  <1100px: workspace derecho colapsa a overlay (togglable con botón).
  ≥1100px: 3 paneles visibles simultáneamente.

No hay top bar de GNOME. No hay activities. No hay tray. Solo Hermes.

Novedades US3/US4 (spec 011):
  - NoModelBanner (banner propio) cuando no hay provider activo; descartable con "Más tarde".
  - Autoscroll inteligente + FAB "↓ nuevos mensajes".
  - ChatTurnStateMachine controla Enviar↔Detener, typing, caret.
  - Reintento automático del último mensaje al conectar un modelo.
  - Chips del empty state rellenan el composer.
  - Señal stop-requested del composer detiene el turno.
  - inference_not_configured → ChatActionCard (no texto plano).
  - pending_approval → ChatActionCard con Aprobar/Rechazar.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from hermes.shell.domain.shell_session import (
    RuntimeLinkState,
    ShellSession,
    ShellView,
)
from hermes.shell.infrastructure.shell_backend_client import (
    ShellBackendClient,
)
from hermes.shell.presentation.gtk4.chat_turn_state import (
    AutoscrollTracker,
    ChatTurnStateMachine,
    TurnState,
)
from hermes.shell.presentation.gtk4.widgets.sidebar import HermesSidebar
from hermes.shell.presentation.gtk4.widgets.composer import HermesComposer
from hermes.shell.presentation.gtk4.widgets.chat_view import HermesChatView
from hermes.shell.presentation.gtk4.widgets.workspace import HermesWorkspace
from hermes.shell.presentation.gtk4.widgets.agent_status import (
    HermesAgentStatusBar,
)
from hermes.shell.presentation.gtk4.widgets.providers_dialog import (
    ProvidersDialog,
)
from hermes.shell.presentation.gtk4.widgets.agent_dialog import AgentDialog
from hermes.shell.presentation.gtk4.widgets.no_model_banner import NoModelBanner
from hermes.shell.presentation.gtk4.layout_prefs import LayoutPrefs
from hermes.shell.presentation.gtk4.approved_sites_store import ApprovedSitesStore

logger = logging.getLogger(__name__)


class HermesShellWindow(Adw.ApplicationWindow):
    """Ventana principal de la Hermes Shell."""

    def __init__(
        self,
        *,
        application: Adw.Application,
        session: ShellSession,
        mock_runtime: bool,
        theme_manager=None,  # ThemeManager | None — pasado desde app.py
    ) -> None:
        super().__init__(application=application)
        self.set_title("Hermes")
        self.set_default_size(1440, 900)
        self.add_css_class("hermes-shell")
        self.set_decorated(True)

        self._session = session
        self._mock_runtime = mock_runtime
        self._client = ShellBackendClient()
        # ThemeManager singleton del proceso. Nunca crear uno nuevo aquí.
        self._theme_manager = theme_manager
        # Preferencias de disposición (persistidas en JSON, independientes del dominio).
        self._layout_prefs = LayoutPrefs()
        # Aplicar densidad persistida al arrancar.
        self._apply_density_class(self._layout_prefs.density)
        self._active_conversation_id: str | None = None
        self._current_agent_bubble = None  # bubble in-progress

        # ApprovedSitesStore — persisted list of hostnames the browser agent
        # may write to. Instantiated once per window; shared with HermesSettingsWindow
        # so the Settings page can add/remove sites and the daemon reads the same data.
        self._approved_sites_store = ApprovedSitesStore()

        # T053/T054: runtime client (lazily wired; None until daemon connects)
        self._runtime_client = None  # DbusRuntimeClient | None
        # asyncio loop for enqueue calls (runs in a dedicated daemon thread)
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._async_thread: threading.Thread | None = None

        # US3: máquina de estados del turno y tracker de autoscroll.
        self._turn_sm = ChatTurnStateMachine()
        self._scroll_tracker = AutoscrollTracker()

        # Referencia al ScrolledWindow del chat (para autoscroll).
        self._chat_scroll: Gtk.ScrolledWindow | None = None

        # FAB "↓ nuevos mensajes" — creado una vez, visible/oculto dinámicamente.
        self._fab: Gtk.Button | None = None

        # NoModelBanner — banner propio (2 acciones: Conectar / Más tarde).
        self._no_model_banner: NoModelBanner | None = None

        # Descarte explícito del usuario ("Más tarde"). Se lee de LayoutPrefs
        # al arrancar; True = el usuario lo descartó voluntariamente.
        self._banner_dismissed: bool = self._layout_prefs.banner_dismissed

        # Última action card de "sin modelo" — para quitarla al reconectar.
        self._no_model_card = None

        self._build_layout()
        self._wire_handlers()
        self._start_async_loop()

    # ----------------------------------------------------------------
    # Layout
    # ----------------------------------------------------------------
    def _build_layout(self) -> None:
        # Root: overlay split = sidebar | resto.
        self._root_split = Adw.OverlaySplitView()
        self._root_split.set_min_sidebar_width(240)
        self._root_split.set_max_sidebar_width(280)
        self._root_split.set_sidebar_width_fraction(0.18)

        # Sidebar.
        self._sidebar = HermesSidebar(
            active=self._session.active_view,
            client=self._client,
        )
        self._root_split.set_sidebar(self._sidebar)

        # Content: segundo OverlaySplitView — center (chat) + workspace.
        self._content_split = Adw.OverlaySplitView()
        self._content_split.set_sidebar_position(Gtk.PackType.END)
        self._content_split.set_min_sidebar_width(320)
        self._content_split.set_max_sidebar_width(420)
        self._content_split.set_sidebar_width_fraction(0.28)
        self._content_split.set_show_sidebar(True)
        self._content_split.set_collapsed(False)

        # Center: status bar + Gtk.Stack.
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        center.add_css_class("hermes-center-pane")
        center.set_hexpand(True)

        self._status_bar = HermesAgentStatusBar(
            link_state=self._session.runtime_link_state
        )
        center.append(self._status_bar)

        # NoModelBanner — banner propio con Conectar ahora + Más tarde.
        self._no_model_banner = NoModelBanner(
            on_connect=self._open_providers_dialog,
            on_dismiss=self._on_banner_dismissed,
        )
        center.append(self._no_model_banner)

        # Minimize button.
        minimize_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        minimize_bar.set_halign(Gtk.Align.END)
        minimize_bar.set_margin_end(8)
        minimize_bar.set_margin_bottom(2)
        _minimize_btn = Gtk.Button.new_from_icon_name("window-minimize-symbolic")
        _minimize_btn.add_css_class("flat")
        _minimize_btn.set_tooltip_text("Minimizar la shell de Hermes")
        _minimize_btn.connect("clicked", lambda _b: self.minimize())
        minimize_bar.append(_minimize_btn)

        self._workspace_toggle_btn = Gtk.Button.new_from_icon_name(
            "sidebar-show-right-symbolic"
        )
        self._workspace_toggle_btn.add_css_class("flat")
        self._workspace_toggle_btn.set_tooltip_text("Mostrar/ocultar workspace")
        self._workspace_toggle_btn.connect(
            "clicked",
            lambda _b: self._content_split.set_show_sidebar(
                not self._content_split.get_show_sidebar()
            ),
        )
        minimize_bar.append(self._workspace_toggle_btn)
        center.append(minimize_bar)

        self._center_stack = Gtk.Stack()
        self._center_stack.set_vexpand(True)
        self._center_stack.set_transition_type(
            Gtk.StackTransitionType.CROSSFADE
        )

        # Vista CHAT.
        chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        from hermes.shell.presentation.gtk4.widgets.chat_header import (
            HermesChatHeader,
        )

        self._active_agent_name = "Hermes"
        self._chat_header = HermesChatHeader(
            client=self._client,
            on_new_chat=self._on_new_chat,
            on_open_conv=self._on_open_conv,
        )
        chat_box.append(self._chat_header)

        # El chat vive en Adw.Clamp(720). El overlay del FAB requiere un
        # Gtk.Overlay que envuelva el ScrolledWindow.
        self._chat = HermesChatView(on_chip_clicked=self._on_chat_chip_clicked)
        chat_scroll = Gtk.ScrolledWindow()
        chat_scroll.set_vexpand(True)
        chat_scroll.set_hexpand(True)
        self._chat_scroll = chat_scroll
        chat_clamp = Adw.Clamp()
        chat_clamp.set_maximum_size(720)
        chat_clamp.set_child(self._chat)
        chat_scroll.set_child(chat_clamp)

        # Overlay para el FAB flotante "↓ nuevos mensajes".
        chat_overlay = Gtk.Overlay()
        chat_overlay.set_vexpand(True)
        chat_overlay.set_hexpand(True)
        chat_overlay.set_child(chat_scroll)

        # FAB — creado aquí, inicialmente oculto.
        self._fab = self._build_fab()
        chat_overlay.add_overlay(self._fab)

        chat_box.append(chat_overlay)

        # El composer también vive en Adw.Clamp(720).
        self._composer = HermesComposer(
            on_model_label_clicked=self._open_providers_dialog,
        )
        composer_clamp = Adw.Clamp()
        composer_clamp.set_maximum_size(720)
        composer_clamp.set_child(self._composer)
        chat_box.append(composer_clamp)

        self._center_stack.add_named(chat_box, "chat")

        # Audit view (F8).
        from hermes.shell.presentation.gtk4.widgets.audit_view import (
            HermesAuditView,
        )

        self._audit_view = HermesAuditView(client=self._client)
        self._center_stack.add_named(self._audit_view, "audit")

        # Skills view (F9).
        from hermes.shell.presentation.gtk4.widgets.skills_view import (
            HermesSkillsView,
        )

        self._skills_view = HermesSkillsView(client=self._client)
        self._center_stack.add_named(self._skills_view, "skills")

        # Integrations view.
        from hermes.shell.presentation.gtk4.widgets.integrations_view import (
            HermesIntegrationsView,
        )

        self._integrations_view = HermesIntegrationsView(client=self._client)
        self._center_stack.add_named(self._integrations_view, "integrations")

        # Tasks view.
        from hermes.shell.presentation.gtk4.widgets.tasks_view import (
            HermesTasksView,
        )

        self._tasks_view = HermesTasksView(client=self._client)
        self._center_stack.add_named(self._tasks_view, "tasks")

        # Remote access view.
        from hermes.shell.presentation.gtk4.widgets.remote_access_view import (
            HermesRemoteAccessView,
        )

        self._remote_view = HermesRemoteAccessView()
        self._center_stack.add_named(self._remote_view, "remote")

        center.append(self._center_stack)

        self._content_split.set_content(center)

        # Workspace (panel derecho).
        self._workspace = HermesWorkspace(client=self._client)
        self._content_split.set_sidebar(self._workspace)

        self._root_split.set_content(self._content_split)
        self.set_content(self._root_split)

        self._add_breakpoints()

    def _build_fab(self) -> Gtk.Button:
        """Construye el FAB flotante "↓ nuevos mensajes"."""
        fab = Gtk.Button(label="↓ nuevos mensajes")
        fab.add_css_class("hermes-fab")
        fab.set_halign(Gtk.Align.END)
        fab.set_valign(Gtk.Align.END)
        fab.set_margin_end(16)
        fab.set_margin_bottom(16)
        fab.set_visible(False)
        fab.set_can_focus(True)  # FAB alcanzable por Tab.
        fab.connect("clicked", self._on_fab_clicked)
        return fab

    def _add_breakpoints(self) -> None:
        # BP-1: sidebar izquierdo → overlay <720px.
        bp_sidebar = Adw.Breakpoint.new(
            Adw.BreakpointCondition.new_length(
                Adw.BreakpointConditionLengthType.MAX_WIDTH,
                720,
                Adw.LengthUnit.PX,
            )
        )
        bp_sidebar.add_setter(self._root_split, "collapsed", True)
        self.add_breakpoint(bp_sidebar)

        # BP-2: workspace derecho → overlay <1100px.
        bp_workspace = Adw.Breakpoint.new(
            Adw.BreakpointCondition.new_length(
                Adw.BreakpointConditionLengthType.MAX_WIDTH,
                1100,
                Adw.LengthUnit.PX,
            )
        )
        bp_workspace.add_setter(self._content_split, "collapsed", True)
        self.add_breakpoint(bp_workspace)

    # ----------------------------------------------------------------
    # Wiring
    # ----------------------------------------------------------------
    def _wire_handlers(self) -> None:
        self._sidebar.connect("view-selected", self._on_view_selected)
        self._sidebar.connect("new-conversation", self._on_sidebar_new_conversation)
        self._sidebar.connect("conversation-selected", self._on_sidebar_conv_selected)
        self._sidebar.connect("create-agent", self._on_create_agent)
        self._sidebar.connect("edit-agent", self._on_edit_agent)
        self._sidebar.connect("delete-agent", self._on_delete_agent)
        self._sidebar.connect("agent-selected", self._on_agent_selected)
        self._composer.connect("message-submitted", self._on_message_submitted)
        self._composer.connect("stop-requested", self._on_stop_requested)

        # Autoscroll: escuchar el Adjustment del chat_scroll.
        if self._chat_scroll is not None:
            vadj = self._chat_scroll.get_vadjustment()
            vadj.connect("value-changed", self._on_scroll_value_changed)
            vadj.connect("changed", self._on_scroll_bounds_changed)

        # Comprobar si hay modelo activo al arrancar.
        threading.Thread(target=self._check_active_provider, daemon=True).start()

    def _on_view_selected(self, _sidebar, view: str) -> None:
        try:
            shell_view = ShellView(view)
        except ValueError:
            logger.warning("unknown view: %s", view)
            return
        self._session.switch_view(shell_view)
        logger.info("view switched: %s", view)

        if shell_view in (ShellView.HOME, ShellView.CHAT, ShellView.WORKSPACE):
            self._center_stack.set_visible_child_name("chat")
        elif shell_view == ShellView.AUDIT:
            self._center_stack.set_visible_child_name("audit")
            self._audit_view._reload()  # type: ignore[attr-defined]
        elif shell_view == ShellView.SKILLS:
            self._center_stack.set_visible_child_name("skills")
            self._skills_view._reload()  # type: ignore[attr-defined]
        elif shell_view == ShellView.INTEGRATIONS:
            self._center_stack.set_visible_child_name("integrations")
            self._integrations_view._reload()  # type: ignore[attr-defined]
        elif shell_view == ShellView.TASKS:
            self._center_stack.set_visible_child_name("tasks")
            self._tasks_view._reload()  # type: ignore[attr-defined]
        elif shell_view == ShellView.REMOTE:
            self._center_stack.set_visible_child_name("remote")
            self._remote_view._reload()  # type: ignore[attr-defined]

        if shell_view == ShellView.SETTINGS:
            self._open_settings_window()

    def _apply_density_class(self, density: str) -> None:
        """Aplica la clase CSS de densidad en la ventana raíz."""
        if density == "compact":
            self.add_css_class("hermes-density-compact")
            self.remove_css_class("hermes-density-comfortable")
        else:
            self.add_css_class("hermes-density-comfortable")
            self.remove_css_class("hermes-density-compact")

    def _open_settings_window(self) -> None:
        """Abre el panel de Ajustes (Adw.PreferencesWindow) con 6 páginas.

        Reutiliza el ThemeManager existente — nunca crea uno nuevo.
        """
        from hermes.shell.presentation.gtk4.widgets.settings_window import (  # noqa: PLC0415
            HermesSettingsWindow,
        )

        dlg = HermesSettingsWindow(
            parent=self,
            theme_manager=self._theme_manager,
            layout_prefs=self._layout_prefs,
            window_ref=self,
            client=self._client,
            runtime_client=self._runtime_client,
            run_async_cb=self._run_async,
            on_provider_active_changed=self._on_provider_active_changed,
            approved_sites_store=self._approved_sites_store,
        )
        dlg.present()

    def _open_providers_dialog(self) -> None:
        """Abre el ProvidersDialog directamente (llamado desde el label del modelo)."""
        dlg = ProvidersDialog(
            parent=self,
            client=self._client,
            on_active_changed=self._on_provider_active_changed,
        )
        dlg.present()

    def _on_provider_active_changed(self, provider) -> None:
        """Callback cuando el provider activo cambia en el ProvidersDialog."""
        has_model = provider is not None
        logger.info("active provider changed: %s", getattr(provider, "alias", None))
        GLib.idle_add(self._gtk_on_provider_changed, has_model)

    def _gtk_on_provider_changed(self, has_model: bool) -> bool:
        """Actualiza la UI cuando cambia el modelo activo (hilo GTK)."""
        if has_model:
            # Auto-ocultar el banner al conectar modelo.
            if self._no_model_banner is not None:
                self._no_model_banner.set_visible(False)
            # Actualizar el label del composer.
            self._composer.refresh_model_label()
            # Reintento automático del último mensaje sin modelo.
            if self._turn_sm.on_model_connected():
                pending = self._turn_sm.clear_pending_no_model()
                if self._no_model_card is not None:
                    self._chat.remove_widget(self._no_model_card)
                    self._no_model_card = None
                if pending:
                    self._submit_text(pending)
        else:
            if not self._banner_dismissed and self._no_model_banner is not None:
                self._no_model_banner.set_visible(True)
        return False

    def _on_sidebar_new_conversation(self, _sidebar) -> None:
        self._session.switch_view(ShellView.HOME)
        self._center_stack.set_visible_child_name("chat")
        self._on_new_chat()
        self._sidebar.recent_conversations.reload()

    def _on_sidebar_conv_selected(self, _sidebar, conv_id: str) -> None:
        self._session.switch_view(ShellView.HOME)
        self._center_stack.set_visible_child_name("chat")
        self._on_open_conv(conv_id)

    # ------------------------------------------------------------------
    # NoModelBanner
    # ------------------------------------------------------------------

    def _check_active_provider(self) -> None:
        """Verifica si hay un provider activo (hilo de fondo) y muestra el banner."""
        try:
            providers = self._client.list_providers()
            active = next((p for p in providers if getattr(p, "is_active", False)), None)
            has_model = active is not None
        except Exception:  # noqa: BLE001
            has_model = False

        def _apply() -> bool:
            if not has_model and not self._banner_dismissed:
                if self._no_model_banner is not None:
                    self._no_model_banner.set_visible(True)
                self._chat.set_no_model_mode(True)
            return False

        GLib.idle_add(_apply)

    def _on_banner_dismissed(self) -> None:
        """El usuario pulsó "Más tarde": persiste el descarte para no acosar."""
        self._banner_dismissed = True
        self._layout_prefs.banner_dismissed = True
        self._layout_prefs.save()

    # ------------------------------------------------------------------
    # Chips del empty state y "Regenerar"
    # ------------------------------------------------------------------

    def _on_chat_chip_clicked(self, text: str) -> None:
        """Maneja los chips del empty state y las acciones especiales."""
        if text == "__connect_model__":
            self._open_providers_dialog()
        elif text == "__regenerate__":
            self._regenerate_last_message()
        else:
            # Chip normal: rellena el composer y lo enfoca.
            self._composer.set_text(text)

    def _regenerate_last_message(self) -> None:
        """Reenvía el último mensaje del usuario, reemplazando el último bubble del agente."""
        last_text = self._turn_sm.last_user_text
        if not last_text or self._turn_sm.is_live:
            return
        self._submit_text(last_text)

    # ------------------------------------------------------------------
    # Autoscroll + FAB
    # ------------------------------------------------------------------

    def _on_scroll_value_changed(self, adjustment) -> None:
        """Notificado cuando el usuario mueve el scroll o el autoscroll lo actualiza."""
        self._scroll_tracker.update(
            value=adjustment.get_value(),
            page_size=adjustment.get_page_size(),
            upper=adjustment.get_upper(),
        )
        self._update_fab_visibility()

    def _on_scroll_bounds_changed(self, adjustment) -> None:
        """Notificado cuando el contenido del scroll cambia de tamaño (nuevo mensaje)."""
        if self._scroll_tracker.stuck_to_bottom:
            self._do_autoscroll(adjustment)

    def _do_autoscroll(self, adjustment) -> None:
        """Scroll suave al fondo. Solo mueve la posición; nunca anima contenido."""
        adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())

    def _update_fab_visibility(self) -> None:
        if self._fab is None:
            return
        should_show = not self._scroll_tracker.stuck_to_bottom
        if should_show != self._fab.get_visible():
            self._fab.set_visible(should_show)

    def _on_fab_clicked(self, _btn) -> None:
        """El usuario pulsó el FAB: scroll al fondo y marcamos stuck."""
        self._scroll_tracker.force_stick()
        if self._chat_scroll is not None:
            adj = self._chat_scroll.get_vadjustment()
            self._do_autoscroll(adj)
        if self._fab is not None:
            self._fab.set_visible(False)

    # ------------------------------------------------------------------
    # Agent management
    # ------------------------------------------------------------------

    def _on_create_agent(self, _sidebar) -> None:
        def on_save(draft: dict) -> None:
            self._run_async(self._async_create_agent(draft))

        dlg = AgentDialog(parent=self, on_save=on_save)
        dlg.present()

    def _on_edit_agent(self, _sidebar, agent_id: str) -> None:
        agent = self._find_agent_by_id(agent_id)

        def on_save(draft: dict) -> None:
            self._run_async(self._async_update_agent(agent_id, draft))

        dlg = AgentDialog(parent=self, on_save=on_save, agent=agent)
        dlg.present()

    def _on_delete_agent(self, _sidebar, agent_id: str) -> None:
        self._run_async(self._async_delete_agent(agent_id))

    def _on_agent_selected(self, _sidebar, agent_id: str) -> None:
        self._run_async(self._async_set_active_agent(agent_id))

    def _find_agent_by_id(self, agent_id: str) -> dict | None:
        selector = self._sidebar.agent_selector
        for a in selector._agents:
            if a.get("agent_id") == agent_id:
                return a
        return None

    # ------------------------------------------------------------------
    # Agent async operations
    # ------------------------------------------------------------------

    async def _async_load_agents(self) -> None:
        client = self._runtime_client
        if client is None:
            return
        try:
            agents = await client.list_agents()
            active_id = await client.get_active_agent()
        except Exception as exc:  # noqa: BLE001
            logger.warning("load agents failed: %s", exc)
            return

        def _apply() -> bool:
            self._sidebar.agent_selector.set_agents(agents, active_id)
            active = next((a for a in agents if a.get("agent_id") == active_id), None)
            self._active_agent_name = (active or {}).get("name") or "Hermes"
            self._chat_header.set_title(self._active_agent_name)
            self._chat.set_greeting(self._active_agent_name)
            return False

        GLib.idle_add(_apply)

    async def _async_set_active_agent(self, agent_id: str) -> None:
        client = self._runtime_client
        if client is None:
            return
        try:
            await client.set_active_agent(agent_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_active_agent failed: %s", exc)
            return
        await self._async_load_agents()

    async def _async_create_agent(self, draft: dict) -> None:
        client = self._runtime_client
        if client is None:
            self._gtk_show_agent_error("El daemon no está disponible. Conéctate primero.")
            return
        try:
            new_agent = await client.create_agent(draft)
        except Exception as exc:  # noqa: BLE001
            logger.warning("create_agent failed: %s", exc)
            self._gtk_show_agent_error(str(exc))
            return
        await self._async_set_active_agent(new_agent["agent_id"])

    async def _async_update_agent(self, agent_id: str, draft: dict) -> None:
        client = self._runtime_client
        if client is None:
            self._gtk_show_agent_error("El daemon no está disponible.")
            return
        try:
            await client.update_agent(agent_id, draft)
        except Exception as exc:  # noqa: BLE001
            logger.warning("update_agent failed: %s", exc)
            self._gtk_show_agent_error(str(exc))
            return
        await self._async_load_agents()

    async def _async_delete_agent(self, agent_id: str) -> None:
        client = self._runtime_client
        if client is None:
            self._gtk_show_agent_error("El daemon no está disponible.")
            return
        try:
            await client.delete_agent(agent_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delete_agent failed: %s", exc)
            self._gtk_show_agent_error(str(exc))
            return
        await self._async_load_agents()

    def _gtk_show_agent_error(self, message: str) -> None:
        def _apply() -> bool:
            self._chat.append_agent_message(f"No se pudo completar la operación: {message}")
            return False

        GLib.idle_add(_apply)

    # ------------------------------------------------------------------
    # Async loop for D-Bus / stream calls
    # ------------------------------------------------------------------

    def _start_async_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._async_loop = loop

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(
            target=_run, daemon=True, name="hermes-shell-async"
        )
        self._async_thread = thread
        thread.start()

    def _run_async(self, coro) -> None:
        if self._async_loop is None:
            logger.warning("async loop not started — cannot schedule coroutine")
            return
        asyncio.run_coroutine_threadsafe(coro, self._async_loop)

    # ------------------------------------------------------------------
    # T029 / T054: runtime state change
    # ------------------------------------------------------------------

    def on_runtime_state_changed(self, new_state: RuntimeLinkState) -> None:
        self._status_bar.set_link_state(new_state)

        if new_state == RuntimeLinkState.CONNECTED:
            self._wire_dbus_client()

    def _wire_dbus_client(self) -> None:
        if self._runtime_client is not None:
            return
        self._run_async(self._connect_dbus_client())

    async def _connect_dbus_client(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import (
            DbusRuntimeClient,
            build_real_dbus_interface,
        )

        try:
            dbus_iface = await build_real_dbus_interface()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "D-Bus proxy connection failed — chat unavailable: %s", exc
            )
            return

        client = DbusRuntimeClient(dbus_interface=dbus_iface)

        def _set_client() -> bool:
            self._runtime_client = client
            logger.info("DbusRuntimeClient wired to real system bus")
            self._run_async(self._async_load_agents())
            return False

        GLib.idle_add(_set_client)

    # ------------------------------------------------------------------
    # T054: message submission via enqueue + task stream
    # ------------------------------------------------------------------

    def _on_message_submitted(self, _composer, text: str) -> None:
        """Entrada principal de mensajes del usuario."""
        self._submit_text(text)

    def _submit_text(self, text: str) -> None:
        """Pinta el bubble de usuario (optimista) y encola la tarea.

        Si no hay modelo activo → muestra ChatActionCard y guarda para reintento.
        Si no hay conexión → mensaje "no disponible".
        """
        # Envío optimista: bubble usuario <100ms.
        self._chat.append_user_message(text)
        self._scroll_tracker.force_stick()

        # Actualizar la máquina de estados.
        self._turn_sm.on_user_message(text)

        # Verificar si hay modelo activo (hilo de fondo no bloquea la UI).
        threading.Thread(
            target=self._check_model_and_submit,
            args=(text,),
            daemon=True,
        ).start()

    def _check_model_and_submit(self, text: str) -> None:
        """Verifica el modelo en hilo de fondo; vuelve al hilo GTK para continuar."""
        has_model = False
        try:
            providers = self._client.list_providers()
            active = next((p for p in providers if getattr(p, "is_active", False)), None)
            has_model = active is not None
        except Exception:  # noqa: BLE001
            has_model = False

        GLib.idle_add(self._gtk_continue_submit, text, has_model)

    def _gtk_continue_submit(self, text: str, has_model: bool) -> bool:
        """Continuación del envío en el hilo GTK."""
        if not has_model:
            # Sin modelo: guardar para reintento + mostrar ChatActionCard.
            self._turn_sm.on_no_model_sent(text)
            self._no_model_card = self._chat.append_action_card(
                message="Aún no he podido responder. Conecta un servicio desde ajustes y vuelve aquí.",
                button_label="Conectar ahora",
                on_action=self._open_providers_dialog,
            )
            # Mostrar el banner si no estaba descartado explícitamente.
            if not self._banner_dismissed and self._no_model_banner is not None:
                self._no_model_banner.set_visible(True)
            self._composer.set_turn_in_flight(False)
            return False

        if self._session.runtime_link_state not in (
            RuntimeLinkState.CONNECTED,
            RuntimeLinkState.DEGRADED,
        ):
            self._chat.append_agent_unavailable()
            self._turn_sm.on_enqueue_fail()
            self._composer.set_turn_in_flight(False)
            return False

        if self._runtime_client is None:
            self._wire_dbus_client()

        was_new = self._active_conversation_id is None
        if was_new:
            import uuid
            self._active_conversation_id = str(uuid.uuid4())

        # Iniciar el bubble de streaming con typing indicator.
        bubble = self._chat.start_streaming_agent_message()
        self._current_agent_bubble = bubble
        self._status_bar.set_agent_activity("thinking")

        # Actualizar máquina de estados y botón del composer.
        self._turn_sm.on_enqueue_ok()
        self._composer.set_turn_in_flight(True)

        self._run_async(
            self._enqueue_and_stream(
                text=text,
                conversation_id=self._active_conversation_id,
                bubble=bubble,
            )
        )
        return False

    def _on_stop_requested(self, _composer) -> None:
        """El usuario pulsó Detener o Esc — finaliza el turno en vuelo."""
        bubble = self._current_agent_bubble
        if bubble is not None:
            self._chat.finalize_streaming_bubble(bubble)
        self._current_agent_bubble = None
        self._turn_sm.on_stop()
        self._composer.set_turn_in_flight(False)
        self._status_bar.set_agent_activity("idle")

    async def _enqueue_and_stream(
        self,
        *,
        text: str,
        conversation_id: str,
        bubble,
    ) -> None:
        client = self._runtime_client
        if client is None:
            self._gtk_stream_error(bubble, "El agente no está disponible en este momento.")
            return

        try:
            task_id, stream_path = await client.enqueue(
                kind="chat_message",
                text=text,
                conversation_id=conversation_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("enqueue failed: %s", exc)
            self._gtk_enqueue_failed(bubble)
            return

        logger.info("enqueued task_id=%s stream_path=%s", task_id, stream_path)

        stream = client.subscribe_task_stream(stream_path=stream_path)
        await self._consume_stream(stream, bubble)

    async def _consume_stream(self, stream, bubble) -> None:
        try:
            async for frame in stream.frames():
                self._gtk_apply_frame(frame, bubble)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stream ended with error: %s", exc)
            self._gtk_stream_interrupted(bubble)

    def _gtk_apply_frame(self, frame, bubble) -> None:
        """Bridge de un frame del stream al hilo GTK."""
        from hermes.shell.infrastructure.dbus_fast_runtime_client import StreamFrame

        def _apply() -> bool:
            kind = frame.kind
            payload = frame.payload

            if kind == "delta":
                self._status_bar.set_agent_activity("acting")
                # Primera vez: transición AWAITING → STREAMING en la máquina.
                if self._turn_sm.state == TurnState.AWAITING_FIRST_TOKEN:
                    self._turn_sm.on_first_delta()
                else:
                    self._turn_sm.on_delta()
                if bubble is not None:
                    self._chat.append_delta_to_streaming_bubble(
                        bubble, payload.get("delta", "")
                    )

            elif kind == "thinking_delta":
                self._status_bar.set_agent_activity("thinking")
                # El thinking_delta también cuenta como "primer token".
                if self._turn_sm.state == TurnState.AWAITING_FIRST_TOKEN:
                    self._turn_sm.on_first_delta()
                # El texto de thinking no se muestra en el bubble de usuario
                # (permanece oculto en la UI, como Claude web).

            elif kind == "tool_call":
                tc = payload.get("tool_call") or {}
                name = tc.get("name", tc.get("function", {}).get("name", "tool"))
                args = str(tc.get("args", tc.get("arguments", "")))[:200]
                self._chat.append_tool_call(tool_name=name, payload_preview=args)
                self._status_bar.set_agent_activity("acting")
                self._turn_sm.on_tool_call()

            elif kind == "error":
                err = payload.get("error", "Error desconocido")
                self._turn_sm.on_error_frame()
                self._composer.set_turn_in_flight(False)
                if err == "inference_not_configured":
                    # Tarjeta de acción en vez de texto plano.
                    self._chat.finalize_streaming_bubble(bubble)
                    self._no_model_card = self._chat.append_action_card(
                        message="Aún no he podido responder. Conecta un servicio desde ajustes y vuelve aquí.",
                        button_label="Conectar ahora",
                        on_action=self._open_providers_dialog,
                    )
                else:
                    self._gtk_show_retry_card(bubble, err)
                self._current_agent_bubble = None
                self._status_bar.set_agent_activity("idle")

            elif kind == "status":
                status = payload.get("status", "")
                if status == "pending_approval":
                    self._turn_sm.on_approval_needed()
                    self._status_bar.set_agent_activity("waiting")
                    # Tarjeta de aprobación con botones Aprobar/Rechazar.
                    # Por ahora muestra la tarjeta; la integración de Aprobar/Rechazar
                    # requiere el protocolo HITL del broker (roadmap).
                    self._chat.append_action_card(
                        message="El agente solicita tu aprobación antes de continuar.",
                        button_label="Aprobar",
                        on_action=lambda: logger.info("approval granted"),
                    )

            elif kind == "done":
                self._turn_sm.on_done()
                self._composer.set_turn_in_flight(False)
                self._status_bar.set_agent_activity("idle")
                if bubble is not None:
                    self._chat.finalize_streaming_bubble(bubble)
                self._current_agent_bubble = None

            return False

        GLib.idle_add(_apply)

    def _gtk_show_retry_card(self, bubble, error_message: str) -> None:
        """Finaliza el bubble y añade tarjeta Reintentar."""
        def _apply() -> bool:
            if bubble is not None:
                self._chat.finalize_streaming_bubble(bubble)

            last_text = self._turn_sm.last_user_text

            def _retry() -> None:
                if last_text:
                    self._turn_sm.on_retry()
                    self._submit_text(last_text)

            self._chat.append_action_card(
                message=f"Algo salió mal: {error_message}",
                button_label="Reintentar",
                on_action=_retry,
            )
            self._current_agent_bubble = None
            self._status_bar.set_agent_activity("idle")
            return False

        GLib.idle_add(_apply)

    def _gtk_enqueue_failed(self, bubble) -> None:
        """El enqueue falló: añade tarjeta de reintento."""
        def _apply() -> bool:
            if bubble is not None:
                self._chat.finalize_streaming_bubble(bubble)
            self._turn_sm.on_enqueue_fail()
            self._composer.set_turn_in_flight(False)
            self._current_agent_bubble = None

            last_text = self._turn_sm.last_user_text

            def _retry() -> None:
                if last_text:
                    self._turn_sm.on_retry()
                    self._submit_text(last_text)

            self._chat.append_action_card(
                message="No se pudo enviar el mensaje. El agente puede no estar disponible.",
                button_label="Reintentar",
                on_action=_retry,
            )
            self._status_bar.set_agent_activity("idle")
            return False

        GLib.idle_add(_apply)

    def _gtk_stream_error(self, bubble, message: str) -> None:
        def _apply() -> bool:
            if bubble is not None:
                self._chat.finalize_streaming_bubble(bubble)
            self._turn_sm.on_error_frame()
            self._composer.set_turn_in_flight(False)
            self._current_agent_bubble = None
            self._chat.append_agent_message(message)
            self._status_bar.set_agent_activity("idle")
            return False

        GLib.idle_add(_apply)

    def _gtk_stream_interrupted(self, bubble) -> None:
        def _apply() -> bool:
            if bubble is not None:
                self._chat.finalize_streaming_bubble(bubble)
            self._turn_sm.on_stream_interrupted()
            self._composer.set_turn_in_flight(False)
            self._current_agent_bubble = None
            last_text = self._turn_sm.last_user_text

            def _retry() -> None:
                if last_text:
                    self._turn_sm.on_retry()
                    self._submit_text(last_text)

            self._chat.append_action_card(
                message=(
                    "La conexión con el agente se interrumpió. "
                    "El agente sigue trabajando; el resultado estará disponible "
                    "cuando se restablezca la conexión."
                ),
                button_label="Reintentar",
                on_action=_retry,
            )
            self._status_bar.set_agent_activity("idle")
            return False

        GLib.idle_add(_apply)

    # ------------------------------------------------------------------
    # F4.4: New chat + open existing conversation
    # ------------------------------------------------------------------
    def _on_new_chat(self) -> None:
        self._active_conversation_id = None
        self._current_agent_bubble = None
        self._turn_sm = ChatTurnStateMachine()  # reset completo
        self._scroll_tracker = AutoscrollTracker()
        self._no_model_card = None
        self._chat.clear()
        self._chat_header.set_title(self._active_agent_name)
        self._chat.set_greeting(self._active_agent_name)
        self._composer.set_turn_in_flight(False)

    def _on_open_conv(self, conv_id: str) -> None:
        def runner() -> None:
            try:
                detail = self._client.get_conversation(conversation_id=conv_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("open conv: %s", exc)
                return

            def render() -> bool:
                self._chat.clear()
                self._chat_header.set_title(self._active_agent_name)
                self._active_conversation_id = conv_id
                self._turn_sm = ChatTurnStateMachine()
                for m in detail.get("messages", []):
                    if m["role"] == "user":
                        self._chat.append_user_message(m["content"])
                    elif m["role"] == "assistant":
                        self._chat.append_agent_message(m["content"])
                return False

            GLib.idle_add(render)

        threading.Thread(target=runner, daemon=True).start()
