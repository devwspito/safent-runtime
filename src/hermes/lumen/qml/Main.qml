import QtQuick
import QtQuick.Window
import QtQuick.Layouts
import "."

// Lumen shell — navigation host. Top bar + wallpaper + view loader + dock.
Window {
    id: root
    visible: true
    visibility: Window.FullScreen
    width: 1280; height: 800
    color: Theme.bg0
    title: "Lumen"

    property int currentView: 0
    property bool showingOnboarding: backend.needsOnboarding

    // Cross-view hand-off bus: Home (or any view) drops a message here and
    // navigates to Chat; ChatView consumes it on shell assignment. Without
    // this, swapping the view Loader destroys the sender and the message is
    // lost (the single-Loader nav has no shared intent channel otherwise).
    property string pendingMessage: ""

    // icon  = dim SVG path (inactive)
    // iconA = accent SVG path (active)
    readonly property var views: [
        { icon: "icons/home-dim.svg",            iconA: "icons/home-accent.svg",            name: "Inicio",        src: "HomeView.qml" },
        { icon: "icons/message-circle-dim.svg",  iconA: "icons/message-circle-accent.svg",  name: "Chat",          src: "ChatView.qml" },
        { icon: "icons/globe-dim.svg",            iconA: "icons/globe-dim.svg",              name: "Navegador",     src: "BrowserView.qml" },
        { icon: "icons/folder-dim.svg",           iconA: "icons/folder-accent.svg",          name: "Archivos",      src: "FilesView.qml" },
        { icon: "icons/terminal-dim.svg",         iconA: "icons/terminal-accent.svg",        name: "Terminal",      src: "TerminalView.qml" },
        { icon: "icons/list-checks-dim.svg",      iconA: "icons/list-checks-accent.svg",     name: "Tareas",        src: "TasksView.qml" },
        { icon: "icons/shield-check-dim.svg",     iconA: "icons/shield-check-accent.svg",    name: "Seguridad",     src: "SecurityView.qml" },
        { icon: "icons/settings-dim.svg",         iconA: "icons/settings-accent.svg",        name: "Ajustes",       src: "SettingsView.qml" },
        { icon: "icons/cpu-dim.svg",              iconA: "icons/cpu-dim.svg",                name: "Conecta tu IA", src: "ConnectAIView.qml" }
    ]
    function go(i) { currentView = i }

    // Route a prompt into the Chat view. Sets the hand-off bus, then navigates;
    // ChatView reads pendingMessage when the shell is injected and sends it.
    function askInChat(text) {
        if (!text || text.length === 0) return
        pendingMessage = text
        currentView = 1
    }

    Connections {
        target: backend
        function onNeedsOnboardingChanged() {
            if (!backend.needsOnboarding) root.showingOnboarding = false
        }
    }

    // ── wallpaper — vertical gradient: bg0 top → bgBottom bottom ────────
    // Free on software render (no GPU blur). Adds depth without Canvas.
    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.bg0 }
            GradientStop { position: 1.0; color: Theme.bgBottom }
        }
    }

    // ── active view ───────────────────────────────────────────────────────
    Loader {
        id: viewLoader
        visible: !root.showingOnboarding
        anchors { left: parent.left; right: parent.right; top: parent.top; bottom: parent.bottom
                  topMargin: 44; bottomMargin: 88 }
        source: root.showingOnboarding ? "" : root.views[root.currentView].src
        onLoaded: { if (item && ("shell" in item)) item.shell = root }
    }

    // ── top bar — flat semi-transparent fill + single hairline bottom ─────
    Rectangle {
        visible: !root.showingOnboarding
        anchors { top: parent.top; left: parent.left; right: parent.right }
        height: 44
        color: Theme.mode === "light"
               ? Theme.alpha("#F5F5F7", 0.96)
               : Theme.alpha("#0B0B0D", 0.96)

        // Bottom hairline separator
        Rectangle {
            anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
            height: 1; color: Theme.line
        }

        RowLayout {
            anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
            spacing: Theme.sp1

            // Lumen identity mark — SVG logotype square
            Rectangle {
                width: 24; height: 24; radius: Theme.rSm - 2
                color: Theme.alpha(Theme.accent, 0.90)
                border.color: Theme.alpha(Theme.accentBright, 0.40); border.width: 1

                Image {
                    anchors.centerIn: parent
                    width: 14; height: 14
                    source: "icons/lumen-mark-white.svg"
                    fillMode: Image.PreserveAspectFit
                    smooth: true; mipmap: true
                }
            }

            Text {
                text: "Lumen"
                color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsCaption + 1
                font.weight: Font.DemiBold
                leftPadding: 2
            }

            // Separator dot
            Rectangle {
                width: 3; height: 3; radius: 2
                color: Theme.ink4
                Layout.leftMargin: 4
            }

            Text {
                text: root.views[root.currentView].name
                color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption + 1
            }

            // Connection status dot
            Rectangle {
                width: 6; height: 6; radius: 3
                color: backend.connected ? Theme.ok : Theme.warn
                Layout.leftMargin: Theme.sp1
                Layout.alignment: Qt.AlignVCenter
            }

            Item { Layout.fillWidth: true }

            // Security status — icon + label
            RowLayout {
                spacing: 5
                Image {
                    width: 12; height: 12
                    source: Theme.dimIcon("icons/shield-check-dim.svg")
                    fillMode: Image.PreserveAspectFit
                    smooth: true; mipmap: true
                    opacity: 0.70
                }
                Text {
                    text: "Protegido"
                    color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro
                }
            }

            // Clock
            Text {
                text: backend.clock
                color: Theme.ink2; font.family: Theme.font; font.pixelSize: Theme.tsCaption
                font.weight: Font.Medium
                Layout.leftMargin: Theme.sp2
            }
        }
    }

    // ── dock — macOS-style centered floating pill ─────────────────────────
    Item {
        visible: !root.showingOnboarding
        anchors { horizontalCenter: parent.horizontalCenter; bottom: parent.bottom; bottomMargin: 14 }
        width: dockRow.width + 28
        height: 64

        // Static shadow underlay
        Rectangle {
            anchors { horizontalCenter: parent.horizontalCenter; top: parent.bottom; topMargin: 2 }
            width: parent.width - 20; height: 10; radius: Theme.rXl
            color: "#000000"; opacity: Theme.mode === "light" ? 0.10 : 0.35; z: -1
        }

        // Dock background — flat fill, single hairline border
        Rectangle {
            anchors.fill: parent
            radius: Theme.rXl
            color: Theme.mode === "light"
                   ? Theme.alpha("#FFFFFF", 0.97)
                   : Theme.alpha("#0D0D0F", 0.97)
            border.color: Theme.line; border.width: 1

            // Top hairline inner highlight
            Rectangle {
                anchors { top: parent.top; left: parent.left; right: parent.right }
                anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                height: 1; radius: Theme.rXl - 1
                color: Theme.mode === "light" ? "#000000" : "#FFFFFF"
                opacity: Theme.mode === "light" ? 0.04 : 0.05
            }
        }

        Row {
            id: dockRow
            anchors.centerIn: parent
            spacing: 2

            Repeater {
                model: 8
                Item {
                    id: dockItem
                    width: 56; height: 56
                    property var viewData: root.views[index]
                    property bool isActive: index === root.currentView

                    // Logical group separators
                    Rectangle {
                        visible: index === 2 || index === 5
                        anchors { left: parent.left; leftMargin: -1; verticalCenter: parent.verticalCenter }
                        width: 1; height: 20; color: Theme.line; opacity: 0.70
                    }

                    // Active background — flat accent tint, hairline border
                    Rectangle {
                        visible: dockItem.isActive
                        anchors.centerIn: parent
                        width: 44; height: 44; radius: Theme.rMd
                        color: Theme.alpha(Theme.accent, 0.18)
                        border.color: Theme.alpha(Theme.accentBright, 0.28); border.width: 1
                    }

                    // Icon — accent variant when active, dim when inactive
                    Image {
                        anchors.centerIn: parent
                        width: 20; height: 20
                        source: dockItem.isActive
                                ? Theme.accentIcon(dockItem.viewData.iconA)
                                : Theme.dimIcon(dockItem.viewData.icon)
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                    }

                    // Active indicator dot below icon
                    Rectangle {
                        visible: dockItem.isActive
                        anchors { horizontalCenter: parent.horizontalCenter; bottom: parent.bottom; bottomMargin: 4 }
                        width: 4; height: 4; radius: 2
                        color: Theme.accentBright
                    }

                    MouseArea {
                        anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                        onClicked: root.go(index)
                    }
                }
            }
        }
    }

    // ── onboarding overlay ────────────────────────────────────────────────
    Loader {
        id: onboardingLoader
        anchors.fill: parent
        active: root.showingOnboarding
        sourceComponent: Component {
            OnboardingView {
                anchors.fill: parent
                // Pass the shell reference so the secondary CTA "Conectar un servicio"
                // can deep-link to ConnectAIView (index 8) after finishing.
                shell: root
                onFinished: { root.showingOnboarding = false }
            }
        }
        opacity: root.showingOnboarding ? 1.0 : 0.0
    }
}
