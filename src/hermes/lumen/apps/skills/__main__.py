"""hermes.lumen.apps.skills — Habilidades standalone app.

Entry point: ``python3 -m hermes.lumen.apps.skills``

D-Bus verbs used:
  - ListSkills()              → JSON   (skill registry, polled every 10 s)
  - PromoteSkill(skill_id)             (validated → autonomous — sender_uid auth)
  - DeprecateSkill(skill_id)           (autonomous → deprecated — sender_uid auth)

Governance only. No effectors. No broker calls. No HTTP.

DEPENDENCY NOTE:
  PromoteSkill and DeprecateSkill are declared as required D-Bus verbs (T047).
  Until present the action buttons are rendered but the D-Bus call returns an
  error that is silently swallowed — the list refreshes to show current real state.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_QML_DIR = _HERE.parent.parent / "qml"


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")

    # Inject --gpu-effects so Theme.qml can detect it via Qt.application.arguments.
    if os.environ.get("HERMES_GPU_EFFECTS") == "1" and "--gpu-effects" not in sys.argv:
        sys.argv.append("--gpu-effects")

    _src = str(_QML_DIR.parent.parent.parent)
    if _src not in sys.path:
        sys.path.insert(0, _src)

    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtCore import QUrl

    from hermes.lumen.apps._base.app_main import AppBackend

    app = QGuiApplication(sys.argv)
    app.setApplicationName("Habilidades — Hermes")
    app.setOrganizationName("hermes")

    backend = AppBackend(
        auto_load_keys=["skills"],
        poll_interval_ms=10_000,
    )

    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty("backend", backend)
    ctx.setContextProperty("qmlBaseDir", str(_QML_DIR))

    engine.addImportPath(str(_QML_DIR))

    engine.load(QUrl.fromLocalFile(str(_HERE / "SkillsWindow.qml")))
    if not engine.rootObjects():
        return 1

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
