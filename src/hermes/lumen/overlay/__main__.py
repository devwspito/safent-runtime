"""Hermes overlay — Spotlight-style invocation surface.

Entry point: `python3 -m hermes.lumen.overlay`

Layer-shell strategy (resolved at runtime, best-available):
  1. Qt Wayland layer-shell plugin  (qt6-waylandclient-layer-shell RPM)
     → Pure Qt/QML window using QWaylandLayerSurface.
     → Reuses ChatView.qml + Theme.qml unchanged.
  2. gtk4-layer-shell + GtkWindow   (gtk4-layer-shell RPM)
     → GTK4 window placed on OVERLAY layer; Qt chat embedded as child.
  3. Frameless always-on-top Qt window
     → Dev/noVNC fallback; visible but without true layer-shell guarantees.

The overlay:
  - Appears centered, ~720 px wide, capped at 60% of screen height.
  - Focuses input automatically on show.
  - ESC hides the window (does not terminate the process — T026 keeps it
    resident for sub-100 ms re-invocation).
  - Enter sends the chat message.
  - Uses Runtime1Client (T016) for Enqueue — NEVER calls run_cycle.
  - Consumes /run/hermes/tasks.sock (ChatWorker from lumen/__main__.py).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve the QML base directory so ChatView / Theme can be loaded.
# The overlay module lives at src/hermes/lumen/overlay/__main__.py;
# qml/ is one level up: src/hermes/lumen/qml/
# ---------------------------------------------------------------------------
_LUMEN_DIR = Path(__file__).resolve().parent.parent
_QML_DIR = _LUMEN_DIR / "qml"

# ---------------------------------------------------------------------------
# Layer-shell capability probe (done before importing Qt to avoid display
# requirements during a headless import).
# ---------------------------------------------------------------------------

def _has_qt_layer_shell() -> bool:
    """True when the Qt Wayland layer-shell plugin can be loaded."""
    # Qt uses QPA plugins; the layer-shell plugin ships as
    # libqwayland-client.so + layer-shell extension in Qt 6.5+.
    # We probe by checking the RPM-installed file directly.
    plugin_paths = [
        "/usr/lib64/qt6/plugins/wayland-shell-integration/libqwl-shell.so",
        "/usr/lib/qt6/plugins/wayland-shell-integration/libqwl-shell.so",
        "/usr/lib64/qt6/plugins/wayland-shell-integration/liblayer-shell.so",
    ]
    return any(Path(p).exists() for p in plugin_paths)


def _has_gtk4_layer_shell() -> bool:
    """True when gtk4-layer-shell shared library is present."""
    import ctypes.util  # noqa: PLC0415
    return ctypes.util.find_library("gtk4-layer-shell-0") is not None


# ---------------------------------------------------------------------------
# Backend QObject (shared across all layer-shell strategies)
# ---------------------------------------------------------------------------

from PySide6.QtCore import (
    QObject,
    Property,
    Signal,
    Slot,
    QThread,
    QTimer,
    QUrl,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

# Re-use the stream primitives from the parent lumen package so we don't
# duplicate network code.  These are pure stdlib helpers with no Wayland
# or display dependency.
sys.path.insert(0, str(_LUMEN_DIR.parent.parent.parent))  # src/

from hermes.lumen.__main__ import (  # noqa: E402
    ChatWorker,
    _BACKEND_URL,
    _TASKS_SOCK,
)
from hermes.lumen.dbus_client.runtime1_client import Runtime1Client  # noqa: E402


class OverlayBackend(QObject):
    """QObject exposed to QML as `backend` in the overlay window.

    Provides the same signal surface as lumen/Backend for the subset used
    by ChatView.qml:
      - agentChunk / agentToolEvent / agentDone / agentError  (streaming)
      - connectedChanged / hasActiveModel / clock              (status)

    Transport: Runtime1Client (D-Bus, T016). Never HTTP, never run_cycle.
    """

    # ── Status signals (ChatView compatibility) ──────────────────────────
    connectedChanged = Signal()
    clockChanged = Signal()
    activeProviderChanged = Signal()

    # ── Chat signals (identical to lumen.Backend) ────────────────────────
    agentChunk = Signal(str, str)       # (conversationId, delta)
    agentToolEvent = Signal(str, str)   # (conversationId, jsonEvent)
    agentDone = Signal(str)             # (conversationId)
    agentError = Signal(str, str)       # (conversationId, message)

    # ── Overlay-specific signals ─────────────────────────────────────────
    hideRequested = Signal()            # ESC or send — tell the window to hide

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._connected = False
        self._has_active_model = False
        self._clock = ""
        self._active: dict[str, tuple[QThread, ChatWorker]] = {}

        self._client = Runtime1Client(self)

        # Liveness probe: healthz every 10 s (backed-off; overlay is secondary UI).
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._probe_health)
        self._health_timer.start(10_000)
        self._probe_health()

        import datetime as _dt  # noqa: PLC0415
        self._clock = _dt.datetime.now().strftime("%H:%M")
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._update_clock)
        self._tick.start(10_000)

    # ------------------------------------------------------------------
    # Status properties (ChatView.qml reads these)
    # ------------------------------------------------------------------

    @Property(bool, notify=connectedChanged)
    def connected(self) -> bool:
        return self._connected

    @Property(bool, notify=activeProviderChanged)
    def hasActiveModel(self) -> bool:
        return self._has_active_model

    @Property(str, notify=clockChanged)
    def clock(self) -> str:
        return self._clock

    # ChatView.qml also reads needsOnboarding to show a "connect" banner.
    # In the overlay context it's always False — the full onboarding lives
    # in the main Lumen shell.
    @Property(bool, notify=activeProviderChanged)
    def needsOnboarding(self) -> bool:
        return False

    def _probe_health(self) -> None:
        self._client.healthz(self._on_health)

    def _on_health(self, ok: bool) -> None:
        if ok != self._connected:
            self._connected = ok
            self.connectedChanged.emit()
        if ok:
            self._client.get_active_provider(self._on_active_provider)

    def _on_active_provider(self, raw: str | None) -> None:
        import json as _json  # noqa: PLC0415
        try:
            data = _json.loads(raw) if raw else None
        except (ValueError, TypeError):
            data = None
        new_state = isinstance(data, dict) and bool(data)
        if new_state != self._has_active_model:
            self._has_active_model = new_state
            self.activeProviderChanged.emit()

    def _update_clock(self) -> None:
        import datetime as _dt  # noqa: PLC0415
        now = _dt.datetime.now().strftime("%H:%M")
        if now != self._clock:
            self._clock = now
            self.clockChanged.emit()

    # ------------------------------------------------------------------
    # Chat API — identical slot signature to lumen.Backend.send so
    # ChatView.qml's `backend.send(convId, text)` call works unchanged.
    # ------------------------------------------------------------------

    @Slot(str, str)
    def send(self, conversation_id: str, text: str) -> None:
        """Enqueue a chat_message WorkItem via D-Bus (GATE 0 / M2).

        NEVER calls run_cycle. The daemon's loop drains the queue.
        """
        import hashlib as _h  # noqa: PLC0415
        dedup_key = f"chat:{conversation_id}:{_h.sha256(text.encode()).hexdigest()[:16]}"

        self._client._call(
            "Enqueue",
            ("chat_message", text, 0, dedup_key, conversation_id, ""),
            lambda rv: self._on_enqueue(conversation_id, rv),
            lambda err: self._on_enqueue_error(conversation_id, err),
            multi=True,
        )

    def _on_enqueue(self, conversation_id: str, rv: list) -> None:
        task_id = rv[0] if rv and len(rv) > 0 else ""
        stream_path = rv[1] if rv and len(rv) > 1 else ""
        if not task_id or not stream_path:
            self.agentError.emit(
                conversation_id,
                "El agente no devolvió una ruta de stream válida.",
            )
            self.agentDone.emit(conversation_id)
            return
        self._start_worker(conversation_id, stream_path)

    def _on_enqueue_error(self, conversation_id: str, err: str) -> None:
        msg = err or "El agente no está disponible. Comprueba que el runtime está activo."
        self.agentError.emit(conversation_id, msg)
        self.agentDone.emit(conversation_id)

    def _start_worker(self, conversation_id: str, stream_path: str) -> None:
        self._cleanup_worker(conversation_id)
        thread = QThread(self)
        worker = ChatWorker(
            base_url=_BACKEND_URL,
            stream_path=stream_path,
            conversation_id=conversation_id,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.chunkReceived.connect(self.agentChunk)
        worker.toolEvent.connect(self.agentToolEvent)
        worker.done.connect(self.agentDone)
        worker.error.connect(self.agentError)
        worker.done.connect(lambda _: self._cleanup_worker(conversation_id))
        self._active[conversation_id] = (thread, worker)
        thread.start()

    def _cleanup_worker(self, conversation_id: str) -> None:
        entry = self._active.pop(conversation_id, None)
        if entry is None:
            return
        thread, _ = entry
        thread.quit()
        thread.wait(3000)

    # ------------------------------------------------------------------
    # Overlay lifecycle slots (called by QML or keyboard handler)
    # ------------------------------------------------------------------

    @Slot()
    def hide(self) -> None:
        """Hide the overlay window without terminating the process."""
        self.hideRequested.emit()

    @Slot(str)
    def loadConversation(self, conv_id: str) -> None:
        """No-op stub for ChatView.qml compatibility."""


# ---------------------------------------------------------------------------
# Layer-shell window strategies
# ---------------------------------------------------------------------------

def _run_qt_layer_shell(app: QGuiApplication, backend: OverlayBackend) -> int:
    """Path A: pure Qt/QML with QWaylandLayerSurface.

    Requires the qt6-waylandclient-layer-shell plugin (libqwl-layer-shell.so).
    We set QT_WAYLAND_SHELL_INTEGRATION=layer-shell so Qt picks the right
    shell integration plugin when creating the window.
    """
    os.environ.setdefault("QT_WAYLAND_SHELL_INTEGRATION", "layer-shell")

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.rootContext().setContextProperty("qmlBaseDir", str(_QML_DIR))

    qml_path = Path(__file__).resolve().parent / "OverlayWindow.qml"
    engine.load(QUrl.fromLocalFile(str(qml_path)))
    if not engine.rootObjects():
        return 1

    # Wire hideRequested → window.hide() on the root object.
    root = engine.rootObjects()[0]
    backend.hideRequested.connect(root.hide)

    return app.exec()


def _run_gtk4_layer_shell(backend: OverlayBackend) -> int:
    """Path B: GTK4 window with gtk4-layer-shell, hosting a Qt widget.

    gtk4-layer-shell must call gtk4_layer_shell_init() before gtk_init().
    We spawn a thin GTK4 process that sets layer-shell properties, then
    embeds the Qt overlay via GtkSocket / offscreen texture.

    NOTE: This path is complex and adds a process boundary.  It is
    documented here for completeness; in practice, Path A or Path C-lite
    should be used until a proper GTK4↔Qt embedding solution is validated.
    """
    # Practical fallback: open a GTK4 ApplicationWindow with layer-shell
    # placement and put a GtkBox with a native text entry in it.  The
    # D-Bus call is still made through Runtime1Client in a background thread.
    import ctypes  # noqa: PLC0415
    _lib = ctypes.CDLL("libgtk4-layer-shell-0.so")

    import gi  # noqa: PLC0415
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, GLib  # noqa: PLC0415

    app = Gtk.Application(application_id="os.hermes.overlay")

    def on_activate(gtk_app: Gtk.Application) -> None:
        win = Gtk.ApplicationWindow(application=gtk_app)
        win.set_decorated(False)

        _lib.gtk_layer_init_for_window(win)
        _lib.gtk_layer_set_layer(win, 3)       # GTK_LAYER_SHELL_LAYER_OVERLAY = 3
        _lib.gtk_layer_set_keyboard_mode(win, 1)  # GTK_LAYER_SHELL_KEYBOARD_MODE_EXCLUSIVE
        _lib.gtk_layer_set_anchor(win, 1, True)   # TOP
        _lib.gtk_layer_set_anchor(win, 0, True)   # LEFT
        _lib.gtk_layer_set_anchor(win, 2, True)   # RIGHT
        _lib.gtk_layer_set_margin(win, 1, 120)    # top margin (below top-bar)

        # Simple input bar — full chat reuse over GTK is not in scope here.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(24)
        box.set_margin_end(24)

        label = Gtk.Label(label="Hermes")
        entry = Gtk.Entry()
        entry.set_placeholder_text("Escribe algo a Hermes…")
        entry.set_hexpand(True)

        def on_activate_entry(_entry: Gtk.Entry) -> None:
            text = _entry.get_text().strip()
            if not text:
                return
            import uuid as _uuid  # noqa: PLC0415
            conv_id = str(_uuid.uuid4())
            backend.send(conv_id, text)
            _entry.set_text("")
            win.hide()

        entry.connect("activate", on_activate_entry)

        close_btn = Gtk.Button(label="✕")
        close_btn.connect("clicked", lambda _: win.hide())

        box.append(label)
        box.append(entry)
        box.append(close_btn)

        win.set_child(box)
        win.set_default_size(720, -1)
        win.present()

        # ESC key handler
        ctrl = Gtk.EventControllerKey()
        ctrl.connect("key-pressed", lambda _c, keyval, _kc, _m: win.hide() if keyval == 65307 else None)
        win.add_controller(ctrl)

    app.connect("activate", on_activate)
    return app.run([])


def _run_frameless_qt(app: QGuiApplication, backend: OverlayBackend) -> int:
    """Path C-lite: frameless always-on-top PySide6 window.

    Not true layer-shell but workable for development under noVNC.
    The window uses the same QML as Path A.
    """
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.rootContext().setContextProperty("qmlBaseDir", str(_QML_DIR))

    qml_path = Path(__file__).resolve().parent / "OverlayWindow.qml"
    engine.load(QUrl.fromLocalFile(str(qml_path)))
    if not engine.rootObjects():
        return 1

    root = engine.rootObjects()[0]
    backend.hideRequested.connect(root.hide)

    return app.exec()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    # Force Wayland QPA; the overlay must run as a Wayland client.
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland")

    app = QGuiApplication(sys.argv)
    app.setApplicationName("HermesOverlay")
    app.setApplicationDisplayName("Hermes")

    backend = OverlayBackend()

    if _has_qt_layer_shell():
        return _run_qt_layer_shell(app, backend)

    if _has_gtk4_layer_shell():
        # Path B: GTK4 gtk4-layer-shell. Qt app is already created above
        # but we won't call app.exec() on it; the GTK4 loop runs instead.
        # The OverlayBackend still lives in Qt's event loop via a background
        # thread approach — for now, fall through to Path C-lite since
        # mixing two main loops in one process needs more scaffolding.
        # Document as known limitation; see overlay/__init__.py for the
        # full architectural note.
        pass

    # Path C-lite
    return _run_frameless_qt(app, backend)


if __name__ == "__main__":
    raise SystemExit(main())
