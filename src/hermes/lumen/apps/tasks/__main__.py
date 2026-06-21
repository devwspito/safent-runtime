"""hermes.lumen.apps.tasks — Tareas standalone app.

Entry point: ``python3 -m hermes.lumen.apps.tasks``

D-Bus verbs used (supervision + HITL governance only):
  - ListRecentTasks(limit: int) → JSON   (task queue, polled every 4 s)
  - ListPending(limit: int)     → JSON   (awaiting HITL approval)
  - ApproveAction(proposal_id) → token  (HITL approve — sender_uid auth)
  - RejectAction(proposal_id, reason)   (HITL reject — sender_uid auth)

No effectors. No broker calls. No HTTP.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_QML_DIR = _HERE.parent.parent / "qml"


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")

    # Ensure hermes.lumen is importable (src/ on sys.path).
    _src = str(_QML_DIR.parent.parent.parent)
    if _src not in sys.path:
        sys.path.insert(0, _src)

    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtCore import QUrl

    from hermes.lumen.apps._base.app_main import AppBackend

    app = QGuiApplication(sys.argv)
    app.setApplicationName("Tareas — Hermes")
    app.setOrganizationName("hermes")

    backend = AppBackend(
        auto_load_keys=["recent_tasks", "pending"],
        poll_interval_ms=4_000,
    )

    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty("backend", backend)
    ctx.setContextProperty("qmlBaseDir", str(_QML_DIR))

    # Make Theme singleton and shared components importable from QML.
    engine.addImportPath(str(_QML_DIR))

    engine.load(QUrl.fromLocalFile(str(_HERE / "TasksWindow.qml")))
    if not engine.rootObjects():
        return 1

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
