import QtQuick
import QtQuick.Layouts
import QtQuick.Window
import "../../qml"

// AppWindow — chrome skeleton shared by all 6 standalone capability apps.
//
// Provides:
//   - ApplicationWindow with title bar, min-size constraints, and Theme bg.
//   - A header row (icon + title + subtitle + optional refresh button).
//   - Three overlay states: loading, error, empty — driven by the backend QObject.
//   - A content slot (default property) where the reused view QML goes.
//
// Contract with each app's QML root:
//   - backend must expose: connected (bool), daemonError (string), loading (bool)
//   - title / subtitle / iconSource are passed as properties from the app entrypoint
//     via the QML context (appTitle, appSubtitle, appIcon).
//
// Design rules: Theme tokens only, Lucide line-icons, no emoji, no mock data.

Window {
    id: appWindow

    // Injected from QML context by each app's app_main.py:
    //   appTitle, appSubtitle, appIcon (icon path relative to qml/icons/)
    property string appTitle:    typeof appTitle !== "undefined"    ? appTitle    : "Hermes"
    property string appSubtitle: typeof appSubtitle !== "undefined" ? appSubtitle : ""
    property string appIcon:     typeof appIcon !== "undefined"     ? appIcon     : "icons/sparkles-dim.svg"

    // Content slot — place the reused view QML as a child.
    default property alias viewContent: contentSlot.data

    title: appWindow.appTitle + " — Hermes"

    minimumWidth:  720
    minimumHeight: 480
    width:         960
    height:        640

    // Gradient applied via child Rectangle; Window.color must stay opaque.
    color: Theme.bg0

    // Subtle vertical depth gradient — free on software/VNC render.
    Rectangle {
        anchors.fill: parent
        z: -1
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.bg0 }
            GradientStop { position: 1.0; color: Theme.bgBottom }
        }
    }

    // ── Title bar ────────────────────────────────────────────────────────────
    Rectangle {
        id: titleBar
        anchors { top: parent.top; left: parent.left; right: parent.right }
        height: 56
        color: Theme.surface
        border.color: Theme.line
        border.width: 0

        // Bottom hairline divider
        Rectangle {
            anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
            height: 1
            color: Theme.line
        }

        RowLayout {
            anchors {
                fill: parent
                leftMargin: Theme.sp3
                rightMargin: Theme.sp3
            }
            spacing: Theme.sp2

            // App icon tile
            Rectangle {
                width: 32; height: 32; radius: Theme.rSm
                color: Theme.alpha(Theme.accent, 0.14)
                border.color: Theme.alpha(Theme.accentBright, 0.16)
                border.width: 1

                Image {
                    anchors.centerIn: parent
                    width: 16; height: 16
                    source: "../../qml/" + appWindow.appIcon
                    fillMode: Image.PreserveAspectFit
                    smooth: true; mipmap: true
                }
            }

            // Title + subtitle
            ColumnLayout {
                spacing: 2

                Text {
                    text: appWindow.appTitle
                    color: Theme.ink
                    font.family: Theme.font
                    font.pixelSize: Theme.tsSubtitle
                    font.weight: Font.DemiBold
                }

                Text {
                    visible: appWindow.appSubtitle.length > 0
                    text: appWindow.appSubtitle
                    color: Theme.ink3
                    font.family: Theme.font
                    font.pixelSize: Theme.tsCaption
                }
            }

            Item { Layout.fillWidth: true }

            // Daemon connection indicator
            Rectangle {
                height: 24; radius: Theme.rSm - 2
                implicitWidth: connRow.implicitWidth + 16
                color: backend.connected
                    ? Theme.alpha(Theme.ok, 0.10)
                    : Theme.alpha(Theme.warn, 0.10)
                border.color: backend.connected
                    ? Theme.alpha(Theme.ok, 0.22)
                    : Theme.alpha(Theme.warn, 0.22)
                border.width: 1

                RowLayout {
                    id: connRow
                    anchors.centerIn: parent
                    spacing: 5

                    Rectangle {
                        width: 6; height: 6; radius: 3
                        color: backend.connected ? Theme.ok : Theme.warn
                    }

                    Text {
                        text: backend.connected ? "Conectado" : "Sin daemon"
                        color: backend.connected ? Theme.ok : Theme.warn
                        font.family: Theme.font
                        font.pixelSize: Theme.tsMicro
                        font.weight: Font.Medium
                    }
                }
            }
        }
    }

    // ── Content area ─────────────────────────────────────────────────────────
    Item {
        id: contentArea
        anchors {
            top: titleBar.bottom
            left: parent.left
            right: parent.right
            bottom: parent.bottom
        }

        // ── Loading state ─────────────────────────────────────────────────
        Item {
            anchors.fill: parent
            visible: backend.loading && !backend.daemonError.length

            Column {
                anchors.centerIn: parent
                spacing: Theme.sp2

                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: 48; height: 48; radius: 24
                    color: Theme.alpha(Theme.accent, 0.10)
                    border.color: Theme.alpha(Theme.accentBright, 0.22)
                    border.width: 1

                    Image {
                        anchors.centerIn: parent
                        width: 22; height: 22
                        source: "../../qml/" + Theme.dimIcon("icons/rotate-cw-dim.svg")
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                        opacity: 0.80
                    }
                }

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Cargando…"
                    color: Theme.ink3
                    font.family: Theme.font
                    font.pixelSize: Theme.tsBody
                }
            }
        }

        // ── Error state (daemon unreachable) ──────────────────────────────
        Item {
            anchors.fill: parent
            visible: backend.daemonError.length > 0

            Column {
                anchors.centerIn: parent
                spacing: Theme.sp2
                width: Math.min(contentArea.width - 64, 420)

                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: 56; height: 56; radius: 28
                    color: Theme.alpha(Theme.warn, 0.10)
                    border.color: Theme.alpha(Theme.warn, 0.24)
                    border.width: 1

                    Image {
                        anchors.centerIn: parent
                        width: 24; height: 24
                        source: "../../qml/icons/alert-circle-warn.svg"
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                    }
                }

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Daemon no disponible"
                    color: Theme.ink
                    font.family: Theme.font
                    font.pixelSize: Theme.tsBody
                    font.weight: Font.DemiBold
                }

                Text {
                    width: parent.width
                    horizontalAlignment: Text.AlignHCenter
                    text: backend.daemonError || "Verifica que hermes-runtime está activo."
                    color: Theme.ink3
                    font.family: Theme.font
                    font.pixelSize: Theme.tsCaption
                    wrapMode: Text.WordWrap
                    lineHeight: 1.4
                }
            }
        }

        // ── Real content ─────────────────────────────────────────────────
        Item {
            id: contentSlot
            anchors.fill: parent
            visible: !backend.loading && !backend.daemonError.length
        }
    }
}
