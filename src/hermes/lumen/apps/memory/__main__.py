"""hermes.lumen.apps.memory — Memoria standalone app.

Entry point: ``python3 -m hermes.lumen.apps.memory``

D-Bus verbs used (read-only — T047 DEPENDENCY):
  - ListMemory(limit: int)    → JSON   (NOT YET IMPLEMENTED in daemon)
  - SearchMemory(query: str)  → JSON   (NOT YET IMPLEMENTED in daemon)

HONEST STATE:
  Both verbs are declared as T047 backend dependencies. This app probes
  for them at startup. If the daemon returns an error (method not found),
  ``backend.memoryUnavailable`` is set to True and the QML shows an honest
  "not available yet" state with a T047 reference. NEVER mocked data.

No mutations. No effectors. No broker. No HTTP.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_QML_DIR = _HERE.parent.parent / "qml"


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")

    _src = str(_QML_DIR.parent.parent.parent)
    if _src not in sys.path:
        sys.path.insert(0, _src)

    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtCore import Property, Signal, Slot, QTimer, QUrl

    from hermes.lumen.apps._base.app_main import AppBackend

    class MemoryBackend(AppBackend):
        """AppBackend + ListMemory / SearchMemory with honest unavailable state.

        Probes ListMemory on first connection. If the daemon raises an error
        (method not found), sets memoryUnavailable=True so the QML can show
        an honest state without fabricating data.
        """

        memoryUnavailableChanged = Signal()

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self._memory_unavailable = False
            self.connectedChanged.connect(self._on_connected_for_memory)

        @Property(bool, notify=memoryUnavailableChanged)
        def memoryUnavailable(self) -> bool:
            return self._memory_unavailable

        def _on_connected_for_memory(self) -> None:
            if self.connected:
                self._probe_list_memory()

        def _probe_list_memory(self) -> None:
            """Probe ListMemory — set unavailable flag on error."""
            self._client._call(
                "ListMemory",
                (50,),
                self._on_list_memory_ok,
                self._on_list_memory_err,
            )

        def _on_list_memory_ok(self, raw: str | None) -> None:
            if self._memory_unavailable:
                self._memory_unavailable = False
                self.memoryUnavailableChanged.emit()
            self.listLoaded.emit("memory", raw if raw else "[]")

        def _on_list_memory_err(self, _err: str) -> None:
            if not self._memory_unavailable:
                self._memory_unavailable = True
                self.memoryUnavailableChanged.emit()

        @Slot(str)
        def searchMemory(self, query: str) -> None:
            """SearchMemory (D-Bus) — on-demand search. Emits listLoaded('memory_search', json)."""
            if self._memory_unavailable:
                return
            self._client._call(
                "SearchMemory",
                (query,),
                lambda raw: self.listLoaded.emit("memory_search", raw if raw else "[]"),
                lambda _err: None,
            )

    app = QGuiApplication(sys.argv)
    app.setApplicationName("Memoria — Hermes")
    app.setOrganizationName("hermes")

    backend = MemoryBackend(
        auto_load_keys=[],
        poll_interval_ms=15_000,
    )

    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty("backend", backend)
    ctx.setContextProperty("qmlBaseDir", str(_QML_DIR))

    engine.addImportPath(str(_QML_DIR))

    engine.load(QUrl.fromLocalFile(str(_HERE / "MemoryWindow.qml")))
    if not engine.rootObjects():
        return 1

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
