"""LumenTerminal — the Textual application shell.

Header (StatusBar) + left Sidebar + ContentSwitcher of panes + Footer, with a
command palette and global actions (kill-switch, new chat, auto-mode). Owns the
RuntimeBridge: connects on mount (real D-Bus, falling back to an honest offline
mode), subscribes to daemon signals, and routes them to panes/modals.

Navigation model:
  6 primary entries (sidebar top, always visible) + Avanzado collapsible group.
  Ctrl+K opens the command palette to reach any pane by typing its name.
"""

from __future__ import annotations

import logging

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal
from textual.widgets import ContentSwitcher, Footer

from hermes.tui import messages as M
from hermes.tui.bridge import OfflineRuntimeBridge, RealRuntimeBridge, RuntimeBridge
from hermes.tui.modals.approval import ApprovalModal
from hermes.tui.modals.security_review import SecurityReviewModal
from hermes.tui.screens.agents import AgentsPane
from hermes.tui.screens.chat import ChatPane
from hermes.tui.screens.integrations import IntegrationsPane
from hermes.tui.screens.mcp import McpPane
from hermes.tui.screens.memory import MemoryPane
from hermes.tui.screens.packages import PackagesPane
from hermes.tui.screens.providers import ProvidersPane
from hermes.tui.screens.scheduler import SchedulerPane
from hermes.tui.screens.security import SecurityPane
from hermes.tui.screens.skills import SkillsPane
from hermes.tui.screens.tasks import TasksPane
from hermes.tui.theme import LUMEN_THEME
from hermes.tui.widgets.sidebar import NAV, Sidebar
from hermes.tui.widgets.statusbar import StatusBar

logger = logging.getLogger("hermes.tui.app")


class LumenCommands(Provider):
    """Command palette: jump to any pane + run global actions.

    Bound to Ctrl+K. Every pane — including the Avanzado ones — is reachable
    by typing its name here, so users never have to memorise number shortcuts.
    """

    async def search(self, query: str) -> Hits:
        app: LumenTerminal = self.app  # type: ignore[assignment]
        matcher = self.matcher(query)
        commands: list[tuple[str, str, object]] = []
        for e in NAV:
            commands.append((f"Ir a {e.label}", f"Sección {e.label}", lambda p=e.pane_id: app.go_to(p)))
        commands.append(("Nueva conversación", "Empieza un chat limpio", app.action_new_chat))
        commands.append(("Kill-switch (pausar/reanudar)", "Detén o reanuda al agente", app.action_kill_switch))
        commands.append(("Auto-mode (alternar)", "Modo autónomo on/off", app.action_toggle_auto))
        commands.append(("Refrescar sección", "Recargar datos", app.action_refresh))
        for title, help_text, cb in commands:
            score = matcher.match(title)
            if score > 0:
                yield Hit(score, matcher.highlight(title), cb, help=help_text)


class LumenTerminal(App):
    CSS_PATH = "lumen.tcss"
    TITLE = "Lumen Terminal"
    COMMANDS = {LumenCommands}

    BINDINGS = [
        Binding("ctrl+q", "quit", "Salir"),
        Binding("ctrl+n", "new_chat", "Nueva conversación"),
        Binding("ctrl+k", "kill_switch", "Kill-switch"),
        Binding("ctrl+a", "toggle_auto", "Auto-mode"),
        Binding("ctrl+r", "refresh", "Refrescar"),
        # Primary pane shortcuts (new nav order).
        Binding("1", "go('chat')", "Cerebro", show=False),
        Binding("2", "go('skills')", "Skills", show=False),
        Binding("3", "go('integrations')", "Integraciones", show=False),
        Binding("4", "go('mcp')", "MCP", show=False),
        Binding("5", "go('agents')", "Agentes", show=False),
        Binding("6", "go('tasks')", "Tareas", show=False),
        # Advanced pane shortcuts.
        Binding("7", "go('security')", "Seguridad", show=False),
        Binding("8", "go('scheduler')", "Programador", show=False),
        Binding("9", "go('memory')", "Memoria", show=False),
        Binding("0", "go('providers')", "Proveedores", show=False),
    ]

    def __init__(self, *, bridge: RuntimeBridge | None = None) -> None:
        super().__init__()
        self.bridge: RuntimeBridge = bridge or OfflineRuntimeBridge()
        self._try_real = bridge is None
        self._offline = not self._try_real and isinstance(self.bridge, OfflineRuntimeBridge)
        self._paused = False
        self._auto = False
        self._approval_open = False
        self._has_provider = True

    def compose(self) -> ComposeResult:
        yield StatusBar()
        with Horizontal(id="body"):
            yield Sidebar()
            with ContentSwitcher(initial="pane-chat", id="content"):
                # Primary panes (visible in sidebar by default).
                yield ChatPane()
                yield SkillsPane()
                yield IntegrationsPane()
                yield McpPane()
                yield AgentsPane()
                yield TasksPane()
                # Advanced panes (behind the Avanzado collapsible).
                yield SecurityPane()
                yield SchedulerPane()
                yield MemoryPane()
                yield ProvidersPane()
                yield PackagesPane()
        yield Footer()

    async def on_mount(self) -> None:
        self.register_theme(LUMEN_THEME)
        self.theme = "lumen"
        # Chat is primary — select it and give it focus immediately.
        self.query_one(Sidebar).select_pane("chat")
        self._refresh_header()
        self.run_worker(self._boot(), exclusive=False)

    # ------------------------------------------------------------------
    # Boot: connect bridge, subscribe signals, load header
    # ------------------------------------------------------------------
    async def _boot(self) -> None:
        if self._try_real:
            real = RealRuntimeBridge()
            try:
                await real.connect()
                self.bridge = real
                self._offline = False
            except Exception as exc:  # noqa: BLE001 — daemon down / no dbus-fast
                logger.warning("daemon no disponible, modo offline: %s", exc)
                await self.bridge.connect()
                self._offline = True
        else:
            await self.bridge.connect()
            self._offline = isinstance(self.bridge, OfflineRuntimeBridge)
        self._subscribe_signals()
        await self._load_header()
        # Activate the initially visible pane (chat).
        await self._activate_current()
        if not self._offline and not self._has_provider:
            self.notify(
                "Aún no hay proveedor LLM. Ve a Proveedores (9) → n para añadir uno.",
                title="Configura tu modelo",
                severity="warning",
                timeout=10,
            )

    def _subscribe_signals(self) -> None:
        b = self.bridge
        b.on("chat_delta", lambda c, s, t: self.post_message(M.ChatDelta(c, s, t)))
        b.on("chat_stream_end", lambda c: self.post_message(M.ChatStreamEnd(c)))
        b.on("approval_requested", lambda p: self.post_message(M.ApprovalRequested(p)))
        b.on("agent_liveness_changed", lambda a, h: self.post_message(M.LivenessChanged(a, h)))
        b.on("task_status_changed", lambda t, s, _d: self.post_message(M.TaskStatusChanged(t, s)))
        b.on("scan_completed", lambda i, v: self.post_message(M.ScanCompleted(i, v)))
        b.on("install_review_requested",
             lambda sid, data: self.post_message(M.InstallReviewRequested(sid, data)))

    async def _load_header(self) -> None:
        bar = self.query_one(StatusBar)
        bar.offline = self._offline
        bar.connected = self.bridge.connected
        try:
            agents = await self.bridge.list_agents()
            active = await self.bridge.get_active_agent()
            name = next(
                (a.get("name", "Lumen") for a in agents if str(a.get("id")) == active),
                None,
            )
            if not name:
                name = next((a.get("name") for a in agents if a.get("is_default")), "Lumen")
            bar.agent_name = name or "Lumen"
        except Exception:  # noqa: BLE001
            bar.agent_name = "Lumen"
        try:
            prov = await self.bridge.get_active_provider()
            model = prov.get("model") or prov.get("name") or ""
            self._has_provider = bool(model)
            bar.model_name = model or "sin modelo"
        except Exception:  # noqa: BLE001
            self._has_provider = False
            bar.model_name = "sin modelo"
        try:
            auto = await self.bridge.get_auto_mode()
            self._auto = bool(auto.get("enabled"))
            bar.auto_mode = self._auto
        except Exception:  # noqa: BLE001
            pass
        try:
            qs = await self.bridge.get_queue_status()
            bar.pending = int(qs.get("pending", 0) or 0)
            self._paused = str(qs.get("state")) == "paused"
            bar.paused = self._paused
        except Exception:  # noqa: BLE001
            pass

    def _refresh_header(self) -> None:
        bar = self.query_one(StatusBar)
        bar.offline = self._offline
        bar.connected = self.bridge.connected
        bar.paused = self._paused
        bar.auto_mode = self._auto

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def on_sidebar_navigate(self, event: Sidebar.Navigate) -> None:
        self._switch_to(event.pane_id)

    def action_go(self, pane_id: str) -> None:
        self.go_to(pane_id)

    def go_to(self, pane_id: str) -> None:
        self.query_one(Sidebar).select_pane(pane_id)  # emits Navigate → _switch_to

    def _switch_to(self, pane_id: str) -> None:
        cs = self.query_one("#content", ContentSwitcher)
        target = f"pane-{pane_id}"
        if cs.current != target:
            cs.current = target
        self.run_worker(self._activate_pane(pane_id), exclusive=False)

    async def _activate_current(self) -> None:
        cs = self.query_one("#content", ContentSwitcher)
        if cs.current:
            await self._activate_pane(cs.current.removeprefix("pane-"))

    async def _activate_pane(self, pane_id: str) -> None:
        try:
            pane = self.query_one(f"#pane-{pane_id}")
        except Exception:  # noqa: BLE001
            return
        activate = getattr(pane, "activate", None)
        if activate is not None:
            await activate()

    # ------------------------------------------------------------------
    # Global actions
    # ------------------------------------------------------------------
    def action_new_chat(self) -> None:
        self.go_to("chat")
        self.query_one(ChatPane).new_conversation()

    def action_kill_switch(self) -> None:
        self.run_worker(self._toggle_pause(), exclusive=True)

    async def _toggle_pause(self) -> None:
        try:
            if self._paused:
                await self.bridge.resume()
                self._paused = False
                self.notify("Agente reanudado", timeout=3)
            else:
                await self.bridge.pause("kill-switch del operador")
                self._paused = True
                self.notify("Agente PAUSADO", severity="warning", timeout=3)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Kill-switch falló: {exc}", severity="error", timeout=6)
        self._refresh_header()

    def action_toggle_auto(self) -> None:
        self.run_worker(self._toggle_auto(), exclusive=True)

    async def _toggle_auto(self) -> None:
        try:
            res = await self.bridge.set_auto_mode(not self._auto)
            self._auto = bool(res.get("enabled", not self._auto))
            self.notify(f"Auto-mode {'activado' if self._auto else 'desactivado'}", timeout=3)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo cambiar auto-mode: {exc}", severity="error", timeout=6)
        self._refresh_header()

    def action_refresh(self) -> None:
        self.run_worker(self._activate_current(), exclusive=False)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------
    def on_approval_requested(self, message: M.ApprovalRequested) -> None:
        if self._approval_open:
            return
        self._approval_open = True

        def _done(_result: bool | None) -> None:
            self._approval_open = False

        self.push_screen(ApprovalModal(self.bridge, message.payload_json), _done)

    def on_liveness_changed(self, message: M.LivenessChanged) -> None:
        bar = self.query_one(StatusBar)
        bar.connected = message.alive
        if not message.has_model:
            bar.model_name = "sin modelo"

    def on_scan_completed(self, message: M.ScanCompleted) -> None:
        self.notify(f"Centro de seguridad · escaneo {message.verdict}", timeout=4)

    def on_chat_delta(self, message: M.ChatDelta) -> None:
        try:
            self.query_one(ChatPane).on_chat_delta_signal(message.conversation_id, message.text)
        except Exception:  # noqa: BLE001
            pass

    def on_chat_stream_end(self, message: M.ChatStreamEnd) -> None:
        try:
            self.query_one(ChatPane).on_chat_stream_end_signal(message.conversation_id)
        except Exception:  # noqa: BLE001
            pass

    def on_install_review_requested(self, message: M.InstallReviewRequested) -> None:
        if self._approval_open:
            return
        self._approval_open = True

        def _done(_installed: bool | None) -> None:
            self._approval_open = False

        self.push_screen(
            SecurityReviewModal(self.bridge, message.scan_id, message.scan_data_json), _done
        )

    # ------------------------------------------------------------------
    # Responsive
    # ------------------------------------------------------------------
    def on_resize(self, event) -> None:  # noqa: ANN001
        narrow = event.size.width < 80
        self.set_class(narrow, "narrow")
