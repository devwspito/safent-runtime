"""hermes.lumen.overlay — Spotlight-style layer-shell overlay for Hermes.

Invoked by `python3 -m hermes.lumen.overlay`.

Architecture:
  - gtk4-layer-shell wraps a GTK4 window placed on the wlr-layer-shell
    OVERLAY layer so it floats above all other windows.
  - The chat body is rendered by a PySide6/QML QQuickWidget embedded
    inside a GtkWindow via xdg-foreign / offscreen texture bridge, OR
    (preferred) the overlay is a pure PySide6 window with
    QWaylandLayerSurface (qt-wayland-compositor module).
  - Keyboard shortcuts: Escape dismisses, Enter sends.
  - The overlay enqueues work via Runtime1Client (D-Bus) and consumes
    the task stream from /run/hermes/tasks.sock (ChatWorker).

Layer-shell viability note
--------------------------
PySide6 on Wayland does NOT natively support wlr-layer-shell without a
compositor-side plugin. Two viable paths:

Path A (PREFERRED if qt-wayland/layer-shell plugin is available):
    Use QWaylandLayerSurface via QtWayland plugin
    (qt6-waylandclient + qt6-waylandclient-layer-shell, Fedora packages).
    The window is fully QML, reuses ChatView.qml/Theme.qml unchanged.

Path B (FALLBACK — gtk4-layer-shell + GtkWindow containing QQuickWidget):
    A GTK4 GtkWindow requests layer-shell placement via gtk4-layer-shell.
    A GtkGLArea or socket inside the window hosts the Qt QQuickWidget.
    This adds the gtk4-layer-shell dependency and makes the process
    heavier. Still preferred over reimplementing the chat.

Path C (LAST RESORT):
    Pure GTK4 overlay with native Adwaita widgets, replicating only the
    input bar and a text display area. Loses the QML chat history.

This module implements Path A with a Path B fallback at runtime.
On systems where neither Qt layer-shell nor gtk4-layer-shell is
available, it falls back to a floating always-on-top frameless window
(Path C-lite) so the overlay is still usable in dev/noVNC testing.

The gnome-shell extension (T024) spawns this process via Gio.Subprocess
(or via org.hermes.Runtime1.OpenOverlay if T017 has been merged).
"""
