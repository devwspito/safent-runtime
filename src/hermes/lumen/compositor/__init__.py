"""hermes.lumen.compositor — Wayland compositor for Agents OS Pocket edition.

Provides a minimal Qt/QML Wayland compositor that:
  - Hosts the Lumen shell (Main.qml) as the background scene.
  - Accepts Wayland client connections (browser, terminal) via XDgShell.
  - Exposes a SeatInputAdapter implementing SeatInputEffectorPort so the
    SessionInputBridge security layer is reused unchanged from desktop.
  - Exposes a FramebufferCaptureAdapter implementing ScreenCaptureBackend so
    screenshot requests work via QQuickWindow.grabWindow().

Entry point: `python3 -m hermes.lumen.compositor`
"""
