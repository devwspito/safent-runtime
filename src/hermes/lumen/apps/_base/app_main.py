"""hermes.lumen.apps._base.app_main — reusable standalone-app launcher.

Each capability app (tasks, security, skills, integrations, memory, chat)
calls ``run_app()`` with its specific config.  This module owns:

  - QGuiApplication / QQmlApplicationEngine lifecycle.
  - Runtime1Client setup (shared D-Bus transport from T016).
  - AppBackend QObject: connected, loading, daemonError properties + the
    ``loadList`` / signal surface that the reused views already expect from
    lumen/Backend.
  - QML context wiring: ``backend``, ``appTitle``, ``appSubtitle``, ``appIcon``,
    ``qmlBaseDir`` (absolute path to lumen/qml/ for icon resolution).
  - Wayland/X11 platform detection + QPA env var.

Design rules (non-negotiable):
  - Transport via Runtime1Client only. Zero HTTP.
  - No business logic here: pure presentation plumbing.
  - Authorship = sender_uid derived by the daemon (CWE-862).
  - Never calls run_cycle, CapabilityBroker, or any effector.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

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

# Absolute path to lumen/qml/ — used by QML for icon resolution.
_QML_DIR = Path(__file__).resolve().parent.parent.parent / "qml"
# Absolute path to lumen/apps/ — AppWindow.qml lives in _base/
_APPS_DIR = Path(__file__).resolve().parent.parent

# Re-use stream helpers from the parent lumen package (no display dependency).
_LUMEN_PKG = _QML_DIR.parent
sys.path.insert(0, str(_LUMEN_PKG.parent.parent))  # src/

from hermes.lumen.__main__ import (  # noqa: E402
    ChatWorker,
    _BACKEND_URL,
    _TASKS_SOCK,
)
from hermes.lumen.dbus_client.runtime1_client import Runtime1Client  # noqa: E402

_HEALTH_POLL_MS = 10_000
_PROVIDER_POLL_MS = 5_000   # active-provider fast-poll (matches Backend in __main__.py)
_CLOCK_POLL_MS = 10_000


# ---------------------------------------------------------------------------
# AppBackend — QObject exposed to QML as `backend`
# ---------------------------------------------------------------------------

class AppBackend(QObject):
    """Minimal backend for standalone capability apps.

    Matches the signal/property surface expected by the reused QML views:
      - connected / loading / daemonError
      - listLoaded(key, json)            used by TasksView, SecurityView
      - providersChanged(json)           used by ConnectAIView
      - activeProviderChanged()          used by ConnectAIView
      - providerTestResult(pid, ok, err) used by ConnectAIView
      - agentChunk/Done/Error/ToolEvent  used by ChatView
      - clockChanged / clock             used by ChatView header

    Additionally exposes governance mutators:
      - approve_action / reject_action   (HITL — wired to SecurityView)
      - promote_skill / deprecate_skill  (wired to SkillsView)
      - addProvider / testProvider / activateProvider / listProviders
        (wired to ConnectAIView — IntegrationsBackend subclass)

    All mutations go through Runtime1Client → org.hermes.Runtime1 on the
    system bus.  Authorship = sender_uid (CWE-862).  Zero HTTP.
    """

    # ── Status ───────────────────────────────────────────────────────────
    connectedChanged = Signal()
    loadingChanged = Signal()
    daemonErrorChanged = Signal()
    clockChanged = Signal()

    # ── List data ────────────────────────────────────────────────────────
    listLoaded = Signal(str, str)       # (key, jsonArray)

    # ── Provider signals (ConnectAIView compat) ───────────────────────────
    providersChanged = Signal(str)           # JSON array
    activeProviderChanged = Signal()
    providerTestResult = Signal(str, bool, str)  # (pid, ok, err)

    # ── Chat signals (ChatView compat) ────────────────────────────────────
    agentChunk = Signal(str, str)       # (convId, delta)
    agentToolEvent = Signal(str, str)   # (convId, jsonEvent)
    agentDone = Signal(str)             # (convId)
    agentError = Signal(str, str)       # (convId, message)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        auto_load_keys: list[str] | None = None,
        poll_interval_ms: int = 4_000,
    ) -> None:
        super().__init__(parent)
        self._connected = False
        self._loading = True
        self._daemon_error = ""
        self._clock = ""
        self._has_active_model = False
        self._active_workers: dict[str, tuple[QThread, ChatWorker]] = {}
        self._auto_load_keys = auto_load_keys or []
        self._poll_interval_ms = poll_interval_ms

        self._client = Runtime1Client(self)

        # Liveness probe
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._probe_health)
        self._health_timer.start(_HEALTH_POLL_MS)
        self._probe_health()

        # Active-provider poll — runs independently of health so that the
        # banner "Conecta tu IA" disappears as soon as the user completes
        # onboarding in another app (or if a provider was active before this
        # app opened).  Mirrors the _provider_poll timer in Backend (__main__.py).
        self._provider_timer = QTimer(self)
        self._provider_timer.timeout.connect(self._probe_active_provider)
        self._provider_timer.start(_PROVIDER_POLL_MS)
        # Fire immediately so we don't wait 5 s for the first reading.
        self._probe_active_provider()

        # Clock
        import datetime as _dt
        self._clock = _dt.datetime.now().strftime("%H:%M")
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._update_clock)
        self._tick.start(_CLOCK_POLL_MS)

        # Auto-refresh timer for list views
        if self._auto_load_keys:
            self._refresh_timer = QTimer(self)
            self._refresh_timer.timeout.connect(self._auto_refresh)
            self._refresh_timer.start(self._poll_interval_ms)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @Property(bool, notify=connectedChanged)
    def connected(self) -> bool:
        return self._connected

    @Property(bool, notify=loadingChanged)
    def loading(self) -> bool:
        return self._loading

    @Property(str, notify=daemonErrorChanged)
    def daemonError(self) -> str:
        return self._daemon_error

    @Property(str, notify=clockChanged)
    def clock(self) -> str:
        return self._clock

    @Property(bool, notify=activeProviderChanged)
    def hasActiveModel(self) -> bool:
        return self._has_active_model

    # ChatView reads needsOnboarding — always False in capability apps.
    @Property(bool, notify=activeProviderChanged)
    def needsOnboarding(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # Liveness probe
    # ------------------------------------------------------------------

    def _probe_health(self) -> None:
        self._client.healthz(self._on_health)

    def _on_health(self, ok: bool) -> None:
        if ok != self._connected:
            self._connected = ok
            self.connectedChanged.emit()

        if ok and self._loading:
            self._loading = False
            self._daemon_error = ""
            self.loadingChanged.emit()
            self.daemonErrorChanged.emit()
            self._auto_refresh()
            # Also probe the active provider immediately when the daemon first
            # becomes reachable — avoids a full _PROVIDER_POLL_MS wait on startup.
            self._probe_active_provider()
        elif not ok:
            if not self._daemon_error:
                self._daemon_error = (
                    "El daemon hermes-runtime no está disponible. "
                    "Verifica que el servicio está activo."
                )
                self._loading = False
                self.loadingChanged.emit()
                self.daemonErrorChanged.emit()

    def _update_clock(self) -> None:
        import datetime as _dt
        now = _dt.datetime.now().strftime("%H:%M")
        if now != self._clock:
            self._clock = now
            self.clockChanged.emit()

    # ------------------------------------------------------------------
    # loadList — used by TasksView / SecurityView
    # ------------------------------------------------------------------

    _LIST_VERBS: dict[str, tuple[str, bool]] = {
        "recent_tasks":      ("ListRecentTasks", True),
        "configured_tasks":  ("ListConfiguredTasks", True),
        "pending":           ("ListPending", True),
        "skills":            ("ListSkills", False),
        "agents":            ("ListAgents", False),
    }

    @Slot(str)
    @Slot(str, int)
    def loadList(self, key: str, limit: int = 50) -> None:
        """Load a real list from the daemon and emit listLoaded(key, json)."""
        spec = self._LIST_VERBS.get(key)
        if spec is None:
            self.listLoaded.emit(key, "[]")
            return
        member, takes_limit = spec
        args = (limit,) if takes_limit else ()
        self._client._call(
            member,
            args,
            lambda raw: self.listLoaded.emit(key, raw if raw else "[]"),
            lambda _err: self.listLoaded.emit(key, "[]"),
        )

    def _auto_refresh(self) -> None:
        for key in self._auto_load_keys:
            self.loadList(key)

    # ------------------------------------------------------------------
    # HITL — approve / reject (SecurityView)
    # ------------------------------------------------------------------

    @Slot(str)
    def approveAction(self, proposal_id: str) -> None:
        """ApproveAction via D-Bus. Authorship = sender_uid."""
        self._client.approve_action(
            proposal_id,
            on_reply=lambda _: self.loadList("pending"),
            on_error=lambda err: None,
        )

    @Slot(str, str)
    def rejectAction(self, proposal_id: str, reason: str) -> None:
        """RejectAction via D-Bus."""
        self._client.reject_action(
            proposal_id,
            reason,
            on_reply=lambda _: self.loadList("pending"),
            on_error=lambda _: None,
        )

    # ------------------------------------------------------------------
    # Skills governance (SkillsBackend uses these)
    # ------------------------------------------------------------------

    @Slot(str)
    def promoteSkill(self, skill_id: str) -> None:
        """PromoteSkill via D-Bus (validated → autonomous)."""
        self._client._call(
            "PromoteSkill",
            (skill_id,),
            lambda _: self.loadList("skills"),
            lambda _: None,
        )

    @Slot(str)
    def deprecateSkill(self, skill_id: str) -> None:
        """DeprecateSkill via D-Bus."""
        self._client._call(
            "DeprecateSkill",
            (skill_id,),
            lambda _: self.loadList("skills"),
            lambda _: None,
        )

    # ------------------------------------------------------------------
    # Provider API (ConnectAIView / IntegrationsBackend)
    # ------------------------------------------------------------------

    @Slot()
    def listProviders(self) -> None:
        self._client.list_providers(
            on_reply=lambda raw: self.providersChanged.emit(raw or "[]"),
            on_error=lambda _: self.providersChanged.emit("[]"),
        )

    @Slot(str)
    def activateProvider(self, provider_id: str) -> None:
        self._client._call(
            "SetActiveProvider",
            (provider_id,),
            lambda _: (self.listProviders(), self._probe_active_provider()),
            lambda err: self.providerTestResult.emit("", False, err or "No se pudo activar."),
        )

    @Slot(str, str, str, str)
    def addProvider(
        self,
        provider_kind: str,
        alias: str,
        default_model: str,
        api_key: str,
    ) -> None:
        import json as _json
        draft = _json.dumps({
            "kind": provider_kind,
            "alias": alias,
            "default_model": default_model,
            "api_key": api_key if api_key else None,
            "set_active": False,
        })
        self._client._call(
            "AddProvider",
            (draft,),
            lambda _: self.listProviders(),
            lambda err: self.providerTestResult.emit(
                "", False, err or "No se pudo guardar el proveedor."
            ),
        )

    @Slot(str)
    def testProvider(self, provider_id: str) -> None:
        self._client._call(
            "TestProvider",
            (provider_id,),
            lambda raw: self._on_test_json(provider_id, raw, None),
            lambda err: self._on_test_json(provider_id, None, err),
        )

    def _on_test_json(self, provider_id: str, raw, err) -> None:
        import json as _json
        ok = False
        error_msg = err or ""
        if raw is not None:
            try:
                data = _json.loads(raw)
                ok = bool(data.get("ok", False))
                error_msg = data.get("error") or ""
            except (ValueError, TypeError):
                pass
        self.listProviders()
        self.providerTestResult.emit(provider_id, ok, error_msg)

    def _probe_active_provider(self) -> None:
        import json as _json
        def _on(raw: str | None) -> None:
            try:
                data = _json.loads(raw) if raw else None
            except (ValueError, TypeError):
                data = None
            new_state = isinstance(data, dict) and bool(data)
            if new_state != self._has_active_model:
                self._has_active_model = new_state
                self.activeProviderChanged.emit()
        self._client.get_active_provider(on_reply=_on)

    # ------------------------------------------------------------------
    # Chat API (ChatView)
    # ------------------------------------------------------------------

    @Slot(str, str)
    def send(self, conversation_id: str, text: str) -> None:
        """Enqueue chat_message via D-Bus. Never calls run_cycle."""
        import hashlib as _h
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
        self.agentError.emit(
            conversation_id,
            err or "El agente no está disponible. Verifica que hermes-runtime está activo.",
        )
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
        self._active_workers[conversation_id] = (thread, worker)
        thread.start()

    def _cleanup_worker(self, conversation_id: str) -> None:
        entry = self._active_workers.pop(conversation_id, None)
        if entry is None:
            return
        thread, _ = entry
        thread.quit()
        thread.wait(3000)

    @Slot(str)
    def loadConversation(self, conv_id: str) -> None:
        """No-op stub for ChatView.qml compatibility."""


# ---------------------------------------------------------------------------
# run_app — called by each app's __main__.py
# ---------------------------------------------------------------------------

def run_app(
    *,
    app_id: str,
    title: str,
    subtitle: str,
    icon: str,
    qml_view_file: str,
    auto_load_keys: list[str] | None = None,
    poll_interval_ms: int = 4_000,
    backend_factory: Callable[[], AppBackend] | None = None,
    extra_context: dict | None = None,
) -> int:  # noqa: PLR0913
    """Launch a standalone capability app.

    Args:
        app_id:           Unique reverse-domain ID (e.g. "os.hermes.tasks").
        title:            Window / header title.
        subtitle:         Short description shown in the title bar.
        icon:             Icon path relative to qml/ dir (e.g. "icons/list-checks-dim.svg").
        qml_view_file:    Absolute path to the view's QML file.
        auto_load_keys:   List keys polled automatically (e.g. ["recent_tasks", "pending"]).
        poll_interval_ms: Polling interval for auto_load_keys.
        backend_factory:  Optional callable returning a custom AppBackend subclass.
        extra_context:    Optional dict of extra QML context properties.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")

    # Inject --gpu-effects into argv when HERMES_GPU_EFFECTS=1 so Theme.qml
    # can detect it via Qt.application.arguments without touching process.env.
    if os.environ.get("HERMES_GPU_EFFECTS") == "1" and "--gpu-effects" not in sys.argv:
        sys.argv.append("--gpu-effects")

    app = QGuiApplication(sys.argv)
    app.setApplicationName(title)
    app.setApplicationDisplayName(title)
    app.setOrganizationName("hermes")
    app.setOrganizationDomain("hermes.os")

    backend: AppBackend
    if backend_factory is not None:
        backend = backend_factory()
    else:
        backend = AppBackend(
            auto_load_keys=auto_load_keys,
            poll_interval_ms=poll_interval_ms,
        )

    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty("backend", backend)
    ctx.setContextProperty("appTitle", title)
    ctx.setContextProperty("appSubtitle", subtitle)
    ctx.setContextProperty("appIcon", icon)
    ctx.setContextProperty("qmlBaseDir", str(_QML_DIR))

    if extra_context:
        for key, val in extra_context.items():
            ctx.setContextProperty(key, val)

    # Add the lumen/qml dir to the import path so QML can resolve Theme,
    # ElevatedCard, ListRowButton, etc.
    engine.addImportPath(str(_QML_DIR.parent))  # src/hermes/lumen/ → qml/ visible as "qml"
    engine.addImportPath(str(_QML_DIR))          # direct imports within the same dir

    engine.load(QUrl.fromLocalFile(qml_view_file))
    if not engine.rootObjects():
        return 1

    return app.exec()
