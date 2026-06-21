"""hermes.lumen.apps.integrations — Integraciones standalone app.

Entry point: ``python3 -m hermes.lumen.apps.integrations``

D-Bus verbs used:
  - ListProviders()                        → JSON   (provider list, polled 15 s)
  - GetActiveProvider()                    → JSON   (active model indicator)
  - AddProvider(draft_json)                         (register new LLM provider)
  - TestProvider(provider_id)                       (validate connection)
  - SetActiveProvider(provider_id)                  (activate a provider)

Governance only. Configures which LLM the agent uses — not an agent effector.
No broker calls. No HTTP.
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
    from PySide6.QtCore import QTimer, QUrl

    from hermes.lumen.apps._base.app_main import AppBackend

    class IntegrationsBackend(AppBackend):
        """AppBackend + provider polling for ConnectAIView compatibility."""

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            # Poll active provider on startup and every 15 s.
            self._prov_timer = QTimer(self)
            self._prov_timer.timeout.connect(self._refresh_providers)
            self._prov_timer.start(15_000)
            self.connectedChanged.connect(self._on_connected_for_providers)

        def _on_connected_for_providers(self) -> None:
            if self.connected:
                self._refresh_providers()

        def _refresh_providers(self) -> None:
            if not self.connected:
                return
            self.listProviders()
            self._probe_active_provider()

    app = QGuiApplication(sys.argv)
    app.setApplicationName("Integraciones — Hermes")
    app.setOrganizationName("hermes")

    backend = IntegrationsBackend(
        auto_load_keys=[],  # providers polled directly via _refresh_providers
        poll_interval_ms=15_000,
    )

    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty("backend", backend)
    ctx.setContextProperty("qmlBaseDir", str(_QML_DIR))

    engine.addImportPath(str(_QML_DIR))

    engine.load(QUrl.fromLocalFile(str(_HERE / "IntegrationsWindow.qml")))
    if not engine.rootObjects():
        return 1

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
