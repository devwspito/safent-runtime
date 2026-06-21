"""CompositorApp — sets up QGuiApplication + QQmlApplicationEngine for pocket.

Loads Compositor.qml which hosts both the Lumen shell scene and the Wayland
server for client applications (browser, terminal).

The QPA backend is controlled by QT_QPA_PLATFORM:
  - Production (RK3588, bare KMS): QT_QPA_PLATFORM=eglfs  [H0-HARDWARE]
  - Development (nested Wayland):  QT_QPA_PLATFORM=wayland
  - Headless / CI:                 QT_QPA_PLATFORM=offscreen
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_QML_DIR = Path(__file__).resolve().parent / "qml"
_KEEP_ALIVE: list = []  # mantiene vivos los objetos Python expuestos a QML (sysManager)


def build_application(argv: list[str]) -> "tuple[QGuiApplication, QQmlApplicationEngine]":
    """Construct the Qt application and load Compositor.qml.

    Returns (app, engine). Engine rootObjects() is empty on load failure;
    callers must check.
    """
    from PySide6.QtGui import QFont, QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtCore import QUrl

    app = QGuiApplication(argv)
    app.setApplicationName("Lumen Compositor")
    app.setApplicationDisplayName("Lumen Compositor")

    # Tipografía Lumen: Inter por defecto (refinada). El QML que no fija font.family
    # la hereda; si Inter no está instalada, Qt cae a la del sistema. letterSpacing
    # ligeramente negativo (-0.2px aprox) da el aire premium de lumenso.html.
    _ui_font = QFont("Inter")
    _ui_font.setStyleStrategy(QFont.PreferAntialias)
    _ui_font.setLetterSpacing(QFont.AbsoluteSpacing, -0.2)
    app.setFont(_ui_font)

    engine = QQmlApplicationEngine()

    # sysManager: el desktop QML lo usa para lanzar apps Linux nativas (terminal,
    # LibreOffice, VS Code…) en nuestro compositor Wayland + portapapeles.
    from .sys_manager import SysManager  # noqa: PLC0415

    sys_manager = SysManager()
    engine.rootContext().setContextProperty("sysManager", sys_manager)
    _KEEP_ALIVE.append(sys_manager)

    # hermes: puente D-Bus al daemon (chat, conversaciones/chats-recientes,
    # agentes, providers, tasks). El QML llama hermes.call(reqId, method, argsJson)
    # y escucha la señal hermes.result(reqId, ok, json). Reemplaza el api.js HTTP.
    from .hermes_backend import HermesBackend  # noqa: PLC0415

    hermes_backend = HermesBackend()
    engine.rootContext().setContextProperty("hermes", hermes_backend)
    _KEEP_ALIVE.append(hermes_backend)

    # Cargamos el DESKTOP Lumen adaptado (compositor + dock + ventanas + chat + apps),
    # no el Compositor.qml mínimo anterior.
    qml_path = _QML_DIR / "desktop" / "main.qml"
    engine.load(QUrl.fromLocalFile(str(qml_path)))

    if not engine.rootObjects():
        logger.error(
            "hermes.compositor.qml_load_failed path=%s",
            qml_path,
        )

    return app, engine
