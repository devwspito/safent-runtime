"""hermes.lumen.apps.chat — Chat standalone app.

Entry point: ``python3 -m hermes.lumen.apps.chat``

D-Bus verbs used:
  - Enqueue(trigger_kind, text, priority, dedup_key, conversation_id, "")
    → (task_id: str, stream_path: str)

Stream:
  - /run/hermes/tasks.sock (AF_UNIX, WebSocket-over-socket)
    Consumes delta/done/error frames. ChatWorker from lumen/__main__.py is
    reused unchanged.

The agent loop runs in the daemon. This app only enqueues WorkItems and
renders the response stream. Never calls run_cycle.
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
    from PySide6.QtCore import QUrl

    from hermes.lumen.apps._base.app_main import AppBackend

    app = QGuiApplication(sys.argv)
    app.setApplicationName("Chat — Hermes")
    app.setOrganizationName("hermes")

    # Chat app needs no auto-refreshed lists; it only uses the send/stream path.
    backend = AppBackend(
        auto_load_keys=[],
        poll_interval_ms=30_000,
    )

    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty("backend", backend)
    ctx.setContextProperty("qmlBaseDir", str(_QML_DIR))

    engine.addImportPath(str(_QML_DIR))

    engine.load(QUrl.fromLocalFile(str(_HERE / "ChatAppWindow.qml")))
    if not engine.rootObjects():
        return 1

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
