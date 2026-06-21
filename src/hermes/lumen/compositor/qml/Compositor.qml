// Compositor.qml — Agents OS Pocket Wayland compositor.
//
// Architecture:
//   WaylandCompositor (server) owns a WaylandOutput whose window is a
//   fullscreen QQuickWindow.  That window hosts:
//     1. The Lumen shell (qml/Main.qml) as the background scene.
//     2. ShellSurfaceItems for each XdgSurface Wayland client (agent browser,
//        terminal) stacked above the shell layer.
//
// The WaylandSeat is exposed via the compositor C++/Python backend so
// SeatInputAdapter can inject pointer and keyboard events.
//
// Security: the Wayland socket (/run/user/<uid>/wayland-1 or
// WAYLAND_DISPLAY) is restricted to the hermes-user session.  The bridge
// security layer (token + rate-limit + chord denylist) is unaffected.

import QtQuick
import QtQuick.Window
import QtWayland.Compositor
import QtWayland.Compositor.XdgShell
import "../../qml" as LumenQml

WaylandCompositor {
    id: compositor

    // The single output — fullscreen on the primary screen.
    WaylandOutput {
        id: primaryOutput
        compositor: compositor
        sizeFollowsWindow: true

        window: Window {
            id: compositorWindow
            visible: true
            visibility: Window.FullScreen
            title: "Agents OS"

            // ------------------------------------------------------------------
            // Background layer: Lumen shell (Main.qml).
            // Loaded as a child item so it fills the compositor window but sits
            // below any Wayland client surfaces.
            // ------------------------------------------------------------------
            Loader {
                id: shellLoader
                anchors.fill: parent
                z: 0
                source: "../../qml/Main.qml"
                onStatusChanged: {
                    if (status === Loader.Error) {
                        console.error("[compositor] Failed to load Main.qml:", sourceComponent)
                    }
                }
            }

            // ------------------------------------------------------------------
            // Wayland client surface layer.
            // Each XdgSurface spawned by the agent browser or terminal gets a
            // ShellSurfaceItem here, stacked above the Lumen shell.
            // ------------------------------------------------------------------
            Item {
                id: clientLayer
                anchors.fill: parent
                z: 1

                Repeater {
                    model: xdgShell.surfaces
                    ShellSurfaceItem {
                        shellSurface: modelData
                        // Position at origin by default; the agent controls
                        // window placement via xdg_surface geometry negotiation.
                        // TODO(H0-HARDWARE): implement window management policy
                        // (tiling / maximise) once running on RK3588 display.
                        x: 0
                        y: 0
                        autoCreatePopupItems: true
                        onSurfaceDestroyed: {
                            xdgShell.surfaces.remove(index)
                        }
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // Wayland seat: pointer + keyboard.
    // Exposed as compositor.seat so Python SeatInputAdapter can reference it
    // via engine.rootObjects()[0].property("seat").
    // ------------------------------------------------------------------
    WaylandSeat {
        id: waylandSeat
        compositor: compositor
    }

    property alias seat: waylandSeat

    // ------------------------------------------------------------------
    // XdgShell: standard desktop Wayland shell protocol (xdg-shell v6+).
    // The agent browser (Chromium/QtWebEngine) uses this to create toplevel
    // windows.
    // ------------------------------------------------------------------
    XdgShell {
        id: xdgShell

        onToplevelCreated: function(toplevel, xdgSurface) {
            xdgShell.surfaces.append(xdgSurface)
        }

        property var surfaces: ListModel {}
    }
}
