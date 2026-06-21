import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import QtQuick.Window
// El singleton Theme vive en ../qml (registrado en qml/qmldir como
// `singleton Theme Theme.qml`). OverlayWindow.qml está en overlay/, así que hay
// que importar el directorio hermano para que `Theme` resuelva (igual que
// ChatView.qml hace `import "."`). Sin esto: ReferenceError: Theme is not defined.
import "../qml"

// Hermes Overlay — Spotlight-style invocation surface.
//
// Container window for the overlay. The chat body is the existing
// ChatView.qml loaded via Loader — nothing is reimplemented here.
//
// Layer-shell placement (Path A): set via QWaylandLayerSurface in the
// Qt Wayland plugin; the QML itself does not need to know about it.
//
// Frameless path (Path C-lite): Window.flags set below provide an
// always-on-top, frameless window for dev/noVNC.
//
// Keyboard:
//   ESC    → backend.hide() → window.hide()
//   Enter  → ChatView composer handles it (existing TextInput.onAccepted)
//
// Focus: FocusScope + forceActiveFocus() on show so the composer is
// immediately active without a mouse click.
//
// The `backend` and `qmlBaseDir` context properties are set by __main__.py.

Window {
    id: overlayWindow

    // ── geometry ─────────────────────────────────────────────────────────
    // Width is fixed at 720px (spec NFR-006 Spotlight profile).
    // Height auto-sizes via the chat loader up to 60% of screen height.
    width: 720
    minimumWidth: 480
    maximumWidth: 720

    height: Math.min(Screen.height * 0.60, 600)
    minimumHeight: 200

    // ── window chrome ──────────────────────────────────────────────────
    // Frameless + always-on-top for the frameless fallback path.
    // Layer-shell path ignores these flags (the plugin sets layer policy).
    flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
    color: "transparent"
    visible: false   // hidden at startup; extension shows it

    // ── centering (frameless path only; layer-shell uses anchor+margin) ─
    x: (Screen.width  - width)  / 2
    y: Math.max(48, (Screen.height - height) / 3)  // upper-third, below top-bar

    // ── root container ────────────────────────────────────────────────────
    FocusScope {
        id: root
        anchors.fill: parent
        focus: true

        // Drop shadow underlay (static Rectangle; no layer.enabled / no blur)
        Rectangle {
            anchors.fill: cardContainer
            anchors.margins: -2
            y: cardContainer.y + 3
            radius: Theme.rXl + 2
            color: "#000000"
            opacity: Theme.elevModal.opacity
            z: 0
        }

        // ── card ─────────────────────────────────────────────────────────
        Rectangle {
            id: cardContainer
            anchors.fill: parent
            radius: Theme.rXl
            color: Theme.mode === "light" ? Theme.surface : "#0E0C18"
            border.color: Theme.alpha(Theme.accentBright, 0.18)
            border.width: 1
            clip: true
            z: 1

            // Top-bar: Hermes mark + "Hermes" label + close button
            Rectangle {
                id: topBar
                anchors { top: parent.top; left: parent.left; right: parent.right }
                height: 44
                color: "transparent"
                z: 2

                // Top hairline
                Rectangle {
                    anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
                    height: 1
                    color: Theme.line
                }

                RowLayout {
                    anchors {
                        fill: parent
                        leftMargin: Theme.sp2
                        rightMargin: Theme.sp1
                        topMargin: 0
                        bottomMargin: 0
                    }
                    spacing: Theme.sp1

                    // Hermes mark
                    Rectangle {
                        width: 26; height: 26; radius: 13
                        color: Theme.alpha(Theme.accent, 0.90)
                        border.color: Theme.alpha(Theme.accentBright, 0.30); border.width: 1

                        Image {
                            anchors.centerIn: parent
                            width: 15; height: 15
                            source: Qt.resolvedUrl(qmlBaseDir + "/icons/lumen-mark-white.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                    }

                    Text {
                        text: "Hermes"
                        color: Theme.ink
                        font.family: Theme.font
                        font.pixelSize: Theme.tsCaption + 1
                        font.weight: Font.DemiBold
                    }

                    Item { Layout.fillWidth: true }

                    // Status pill: connected / not connected
                    Rectangle {
                        height: 20; radius: 10
                        color: backend.connected
                               ? Theme.alpha(Theme.ok, 0.14)
                               : Theme.alpha(Theme.ink4, 0.14)
                        implicitWidth: statusLabel.width + 16

                        Text {
                            id: statusLabel
                            anchors.centerIn: parent
                            text: backend.connected ? "En línea" : "Sin conexión"
                            color: backend.connected ? Theme.ok : Theme.ink3
                            font.family: Theme.font
                            font.pixelSize: Theme.tsMicro
                        }
                    }

                    // Close / hide button (accessible: role=Button, keyboard-operable)
                    Rectangle {
                        id: closeBtn
                        width: 28; height: 28; radius: 8
                        color: closeMa.containsMouse
                               ? Theme.alpha(Theme.ink4, 0.20)
                               : "transparent"
                        Accessible.role: Accessible.Button
                        Accessible.name: "Cerrar overlay"
                        Accessible.onPressAction: backend.hide()

                        Behavior on color { ColorAnimation { duration: 120 } }

                        Image {
                            anchors.centerIn: parent
                            width: 13; height: 13
                            source: Qt.resolvedUrl(qmlBaseDir + "/icons/x-dim.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }

                        MouseArea {
                            id: closeMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: backend.hide()
                        }

                        Keys.onReturnPressed: backend.hide()
                        Keys.onSpacePressed:  backend.hide()
                        activeFocusOnTab: true
                    }
                }
            }

            // ── chat body — loaded from the existing ChatView.qml ──────────
            Loader {
                id: chatLoader
                anchors {
                    top: topBar.bottom
                    left: parent.left
                    right: parent.right
                    bottom: parent.bottom
                }
                // Load relative to the lumen qml/ directory.
                // qmlBaseDir is injected by __main__.py as a string path.
                source: Qt.resolvedUrl(qmlBaseDir + "/ChatView.qml")
                asynchronous: false

                onStatusChanged: {
                    if (status === Loader.Ready && item) {
                        // ChatView expects `shell` for navigation; in overlay
                        // mode there is no shell navigation — inject a minimal
                        // shell-stub that silently drops go() calls.
                        item.shell = overlayShellStub
                        // Give the composer focus as soon as the chat loads.
                        item.forceActiveFocus()
                    }
                    if (status === Loader.Error) {
                        console.error("[overlay] ChatView.qml failed to load:", source)
                    }
                }
            }
        }

        // ── keyboard handler ──────────────────────────────────────────────
        Keys.onEscapePressed: backend.hide()

        // Re-focus the chat composer when the overlay regains focus.
        onActiveFocusChanged: {
            if (activeFocus && chatLoader.item) {
                chatLoader.item.forceActiveFocus()
            }
        }
    }

    // Minimal shell stub: ChatView.qml calls shell.go(N) for navigation.
    // In overlay context there are no other views — silently drop.
    QtObject {
        id: overlayShellStub
        property string pendingMessage: ""
        function go(_viewIndex) { /* no-op in overlay */ }
    }

    // Make overlay visible and focused when shown programmatically.
    onVisibleChanged: {
        if (visible) {
            root.forceActiveFocus()
            if (chatLoader.item) {
                chatLoader.item.forceActiveFocus()
            }
        }
    }
}
