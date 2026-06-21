import QtQuick
import QtQuick.Layouts
import "."

// Lumen — Inicio. Command center: ask the agent or open apps directly.
Item {
    id: home
    property var shell: null

    // Banner dismissed state — persisted so it doesn't reappear after reboot.
    // Auto-clears when a service connects (hasActiveModel becomes true).
    property bool bannerDismissed: false

    // Actividad reciente REAL (última tarea del daemon). Vacío = sin actividad.
    property string latestTitle: ""
    property string latestWhen: ""

    function _relTime(iso) {
        if (!iso) return ""
        var t = Date.parse(iso); if (isNaN(t)) return ""
        var s = Math.max(0, Math.floor((Date.now() - t) / 1000))
        if (s < 60) return "hace " + s + " s"
        if (s < 3600) return "hace " + Math.floor(s / 60) + " min"
        if (s < 86400) return "hace " + Math.floor(s / 3600) + " h"
        return "hace " + Math.floor(s / 86400) + " d"
    }
    Connections {
        target: backend
        function onActiveProviderChanged() {
            // A service just connected — fade out the banner immediately
            home.bannerDismissed = true
        }
        function onListLoaded(key, json) {
            if (key !== "recent_tasks") return
            var arr = []; try { arr = JSON.parse(json) } catch (e) { arr = [] }
            if (arr.length > 0) {
                var r = arr[0]
                home.latestTitle = (r.label && r.label.length) ? r.label : (r.trigger_kind || "")
                home.latestWhen = home._relTime(r.enqueued_at) + (r.status ? " · " + r.status : "")
            } else {
                home.latestTitle = ""; home.latestWhen = ""
            }
        }
    }
    Component.onCompleted: backend.loadList("recent_tasks", 1)
    Timer { interval: 5000; running: true; repeat: true; onTriggered: backend.loadList("recent_tasks", 1) }

    Flickable {
        id: homeFlick
        anchors.fill: parent
        contentHeight: homeCol.y + homeCol.childrenRect.height + Theme.sp4
        clip: true
        flickableDirection: Flickable.VerticalFlick
        boundsBehavior: Flickable.StopAtBounds

        WheelScroll { target: homeFlick }

    ColumnLayout {
        id: homeCol
        anchors.horizontalCenter: parent.horizontalCenter
        y: Math.max(Theme.sp4, home.height * 0.09)
        width: 640
        spacing: 0

        // ── agent mark — aperture SVG, static, no animation ───────────────
        Item {
            Layout.alignment: Qt.AlignHCenter
            width: 72; height: 72

            // Barely-there ambient halo — soft static ring (no Canvas)
            Rectangle {
                anchors.centerIn: parent; width: 120; height: 120; radius: 60
                color: Theme.alpha(Theme.accent, Theme.mode === "light" ? 0.06 : 0.10)
            }

            // The SVG mark — same as onboarding
            Image {
                anchors.centerIn: parent
                width: 72; height: 72
                source: Theme.mode === "light" ? "icons/lumen-mark-light.svg" : "icons/lumen-mark.svg"
                fillMode: Image.PreserveAspectFit
                smooth: true; mipmap: true
            }
        }

        Item { height: Theme.sp3; width: 1 }

        // ── greeting ───────────────────────────────────────────────────────
        Text {
            Layout.alignment: Qt.AlignHCenter
            text: "Buenas tardes, Luis."
            color: Theme.ink; font.family: Theme.font
            font.pixelSize: Theme.tsDisplay; font.weight: 300
            font.letterSpacing: -0.5
        }

        Text {
            Layout.alignment: Qt.AlignHCenter
            text: "¿Qué hago por ti?"
            color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsBody
            topPadding: 6
        }

        Item { height: Theme.sp3; width: 1 }

        // ── no-model invitation banner (dismissable) ──────────────────────
        // Neutral/accent tone — NOT an alarm. Descartable y no reaparece.
        Rectangle {
            Layout.fillWidth: true
            radius: Theme.rLg
            // Visible only when: no model AND not dismissed yet
            visible: !backend.hasActiveModel && !home.bannerDismissed
            height: visible ? (noModelRow.height + Theme.sp3) : 0
            color: Theme.alpha(Theme.accent, 0.07)
            border.color: Theme.alpha(Theme.accentBright, 0.22); border.width: 1

            // Fade out when service connects or user dismisses
            opacity: visible ? 1.0 : 0.0
            Behavior on opacity { NumberAnimation { duration: 200; easing.type: Easing.InCubic } }
            Behavior on height   { NumberAnimation { duration: 200; easing.type: Easing.InCubic } }

            RowLayout {
                id: noModelRow
                anchors { left: parent.left; right: parent.right; top: parent.top
                          leftMargin: Theme.sp2; rightMargin: Theme.sp2; topMargin: Theme.sp2 }
                spacing: Theme.sp2

                // Accent icon tile — sparkles, not warning triangle
                Rectangle {
                    width: 34; height: 34; radius: Theme.rSm
                    color: Theme.alpha(Theme.accent, 0.14)
                    border.color: Theme.alpha(Theme.accentBright, 0.18); border.width: 1
                    Image {
                        anchors.centerIn: parent
                        width: 16; height: 16
                        source: Theme.accentIcon("icons/sparkles-accent.svg")
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                    }
                }

                ColumnLayout {
                    spacing: 2; Layout.fillWidth: true
                    Text {
                        text: "Tu asistente está casi listo"
                        color: Theme.ink; font.family: Theme.font
                        font.pixelSize: Theme.tsCaption + 1; font.weight: Font.DemiBold
                    }
                    Text {
                        text: "Conecta una IA para que Lumen pueda ayudarte"
                        color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption
                    }
                }

                Rectangle {
                    height: 32; radius: Theme.rSm
                    implicitWidth: connectBtnTxt.width + Theme.sp2
                    color: Theme.accent
                    Text {
                        id: connectBtnTxt
                        anchors.centerIn: parent
                        text: "Conectar ahora"
                        color: "white"; font.family: Theme.font
                        font.pixelSize: Theme.tsCaption + 1; font.weight: Font.Medium
                    }
                    MouseArea {
                        anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                        onClicked: { if (home.shell) home.shell.go(8) }
                    }
                }

                // Dismiss — "Más tarde"
                Text {
                    text: "Más tarde"
                    color: Theme.ink4; font.family: Theme.font; font.pixelSize: Theme.tsCaption

                    MouseArea {
                        anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            home.bannerDismissed = true
                            if (typeof backend.setSetting === "function") {
                                backend.setSetting("home_banner_dismissed", "true")
                            }
                        }
                    }
                }
            }
        }

        Item {
            visible: !backend.hasActiveModel && !home.bannerDismissed
            height: visible ? Theme.sp2 : 0; width: 1
        }

        // ── command bar ────────────────────────────────────────────────────
        Item {
            Layout.fillWidth: true; height: 58

            // Static shadow
            Rectangle {
                anchors { left: parent.left; right: parent.right; top: parent.top }
                anchors.topMargin: 3; anchors.leftMargin: 1; anchors.rightMargin: -1
                height: parent.height; radius: Theme.rLg
                color: "#000000"; opacity: Theme.mode === "light" ? 0.06 : 0.18
            }

            Rectangle {
                anchors.fill: parent; radius: Theme.rLg
                color: Theme.card2
                border.color: cmdInput.activeFocus ? Theme.alpha(Theme.accentBright, 0.45) : Theme.line
                border.width: 1
                Behavior on border.color { ColorAnimation { duration: 150 } }

                RowLayout {
                    anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp1 }
                    spacing: Theme.sp1

                    // Sparkles icon — accent, not emoji
                    Image {
                        width: 16; height: 16
                        source: Theme.accentIcon("icons/sparkles-accent.svg")
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                        opacity: 0.70
                    }

                    Item {
                        Layout.fillWidth: true; height: 40
                        TextInput {
                            id: cmdInput
                            anchors.fill: parent
                            verticalAlignment: Text.AlignVCenter
                            color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsBody; clip: true
                            onAccepted: {
                                if (text.length > 0 && home.shell) {
                                    home.shell.askInChat(text); text = ""
                                }
                            }
                        }
                        Text {
                            visible: cmdInput.text.length === 0
                            anchors.fill: parent; verticalAlignment: Text.AlignVCenter
                            text: "Pídeme cualquier cosa…"
                            color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsBody
                        }
                    }

                    // Mic button — Lucide mic icon, ghost style
                    Rectangle {
                        width: 34; height: 34; radius: Theme.rSm
                        color: micHover.containsMouse ? Theme.alpha(Theme.surface2, 0.9) : Theme.alpha(Theme.surface2, 0.6)
                        border.color: Theme.line; border.width: 1
                        Behavior on color { ColorAnimation { duration: 120 } }

                        Image {
                            anchors.centerIn: parent
                            width: 15; height: 15
                            source: Theme.dimIcon("icons/mic-dim.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                        MouseArea {
                            id: micHover
                            anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                        }
                    }

                    // Send button — flat accent, arrow-up icon
                    Rectangle {
                        width: 40; height: 40; radius: Theme.rMd
                        color: Theme.accent
                        opacity: cmdInput.text.length > 0 ? 1.0 : 0.50
                        Behavior on opacity { NumberAnimation { duration: 150 } }

                        Image {
                            anchors.centerIn: parent
                            width: 18; height: 18
                            source: "icons/arrow-up-white.svg"
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                        MouseArea {
                            anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                if (cmdInput.text.length > 0 && home.shell) {
                                    home.shell.askInChat(cmdInput.text); cmdInput.text = ""
                                }
                            }
                        }
                    }
                }
            }
        }

        Item { height: Theme.sp2; width: 1 }

        // ── suggestion chips ───────────────────────────────────────────────
        Flow {
            Layout.alignment: Qt.AlignHCenter
            Layout.fillWidth: true
            spacing: Theme.sp1

            Repeater {
                model: ["Resume esta página", "Organiza mis archivos", "Redacta un correo", "Busca en internet"]

                Item {
                    height: 32
                    width: chipTxt.width + Theme.sp3

                    // Static shadow
                    Rectangle {
                        anchors { left: parent.left; right: parent.right; top: parent.top }
                        anchors.topMargin: 2
                        height: parent.height; radius: Theme.rSm
                        color: "#000000"; opacity: Theme.mode === "light" ? 0.05 : 0.12
                    }

                    Rectangle {
                        anchors.fill: parent
                        radius: Theme.rSm
                        color: chipHover.containsMouse ? Theme.card2 : Theme.card
                        border.color: chipHover.containsMouse ? Theme.line2 : Theme.line
                        border.width: 1
                        Behavior on color { ColorAnimation { duration: 100 } }

                        Text {
                            id: chipTxt
                            anchors.centerIn: parent
                            text: modelData; color: Theme.ink2
                            font.family: Theme.font; font.pixelSize: Theme.tsCaption + 1
                        }
                        MouseArea {
                            id: chipHover
                            anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            onClicked: { if (home.shell) home.shell.askInChat(modelData) }
                        }
                    }
                }
            }
        }

        Item { height: Theme.sp3; width: 1 }

        // ── recent activity card (REAL, oculta si no hay actividad) ─────────
        Item {
            visible: home.latestTitle.length > 0
            Layout.fillWidth: true; height: visible ? 60 : 0

            // Static shadow
            Rectangle {
                anchors { left: parent.left; right: parent.right; top: parent.top }
                anchors.topMargin: 3; anchors.leftMargin: 1; anchors.rightMargin: -1
                height: parent.height; radius: Theme.rLg
                color: "#000000"; opacity: Theme.mode === "light" ? 0.06 : 0.18
            }

            Rectangle {
                anchors.fill: parent; radius: Theme.rLg
                color: Theme.card; border.color: Theme.line; border.width: 1

                // Top hairline highlight
                Rectangle {
                    anchors { top: parent.top; left: parent.left; right: parent.right }
                    anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                    height: 1; radius: Theme.rLg - 1
                    color: Theme.mode === "light" ? "#000000" : "#FFFFFF"
                    opacity: Theme.mode === "light" ? 0.03 : 0.04
                }

                RowLayout {
                    anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                    spacing: Theme.sp2

                    // Success icon tile
                    Rectangle {
                        width: 32; height: 32; radius: Theme.rSm
                        color: Theme.alpha(Theme.ok, 0.12)
                        border.color: Theme.alpha(Theme.ok, 0.16); border.width: 1

                        Image {
                            anchors.centerIn: parent
                            width: 16; height: 16
                            source: "icons/circle-check-ok.svg"
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                    }

                    ColumnLayout {
                        spacing: 2
                        Text {
                            text: home.latestTitle
                            color: Theme.ink; font.family: Theme.font
                            font.pixelSize: Theme.tsCaption + 1; font.weight: Font.Medium
                            elide: Text.ElideRight; Layout.maximumWidth: 520
                        }
                        Text {
                            text: home.latestWhen
                            color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro
                        }
                    }

                    Item { Layout.fillWidth: true }

                    // "Ver" link — chevron icon
                    RowLayout {
                        spacing: 3
                        Text {
                            text: "Ver"
                            color: Theme.accentBright; font.family: Theme.font
                            font.pixelSize: Theme.tsCaption + 1; font.weight: Font.Medium
                        }
                        Image {
                            width: 12; height: 12
                            source: Theme.accentIcon("icons/chevron-right-accent.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                    }
                }
            }
        }

        Item { height: Theme.sp2; width: 1 }

        Text {
            Layout.alignment: Qt.AlignHCenter
            text: "También puedes abrir el Navegador, los Archivos o el Terminal desde el dock"
            color: Theme.ink4; font.family: Theme.font; font.pixelSize: Theme.tsMicro
        }
    }
    }
}
