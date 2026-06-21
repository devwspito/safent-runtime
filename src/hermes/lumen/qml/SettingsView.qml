import QtQuick
import QtQuick.Layouts
import "."

// Lumen — Ajustes. Boxed-list macOS style.
// Tema row has a live sun/moon segmented switch that sets Theme.mode.
// NO emoji. NO MultiEffect/blurEnabled. NO RotationAnimator/loops:Infinite.
Item {
    id: settingsView
    property var shell: null

    Rectangle { anchors.fill: parent; color: Theme.bg0 }

    Flickable {
        id: settingsFlick
        anchors.fill: parent
        contentHeight: outerCol.height + Theme.sp4 * 2
        clip: true
        flickableDirection: Flickable.VerticalFlick
        boundsBehavior: Flickable.StopAtBounds

        WheelScroll { target: settingsFlick }

        ColumnLayout {
            id: outerCol
            width: Math.min(parent.width - 64, 660)
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: 0
            y: Theme.sp4

            // Page title
            Text {
                text: "Ajustes"
                color: Theme.ink
                font.family: Theme.font
                font.pixelSize: 32
                font.weight: Font.Light
                font.letterSpacing: -0.5
                Layout.leftMargin: 4
            }

            Item { height: Theme.sp3; width: 1 }

            // ── Apariencia ─────────────────────────────────────────────────────
            Text {
                text: "Apariencia"
                color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro; font.weight: Font.Medium
                Layout.leftMargin: 6; Layout.bottomMargin: Theme.sp1
            }

            Item {
                Layout.fillWidth: true
                height: appearanceCol.height

                Rectangle {
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    anchors.topMargin: 3; anchors.leftMargin: 2; anchors.rightMargin: -2
                    height: parent.height; radius: Theme.rLg
                    color: "#000000"
                    opacity: Theme.mode === "light" ? 0.06 : 0.16
                }

                Rectangle {
                    anchors.fill: parent; radius: Theme.rLg; color: Theme.card
                    border.color: Theme.line; border.width: 1

                    Rectangle {
                        anchors { top: parent.top; left: parent.left; right: parent.right }
                        anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                        height: 1; radius: Theme.rLg - 1
                        color: Theme.mode === "light" ? "#000000" : "#FFFFFF"
                        opacity: Theme.mode === "light" ? 0.03 : 0.06
                    }
                }

                ColumnLayout {
                    id: appearanceCol
                    anchors { left: parent.left; right: parent.right }
                    spacing: 0

                    // Color de acento
                    Item {
                        height: 64; Layout.fillWidth: true

                        RowLayout {
                            anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                            spacing: Theme.sp2

                            Rectangle {
                                width: 34; height: 34; radius: Theme.rSm
                                color: Theme.alpha(Theme.accent, 0.14)

                                Image {
                                    anchors.centerIn: parent; width: 16; height: 16
                                    source: Theme.accentIcon("icons/palette-accent.svg")
                                    fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                                }
                            }

                            Text {
                                text: "Color de acento"; color: Theme.ink; font.family: Theme.font
                                font.pixelSize: Theme.tsBody; font.weight: Font.Medium; Layout.fillWidth: true
                            }

                            Row {
                                spacing: Theme.sp1 - 2
                                property int selected: 0

                                Repeater {
                                    model: [
                                        { color: "#6E56CF" },
                                        { color: "#2563EB" },
                                        { color: "#14B8A6" },
                                        { color: "#FF375F" },
                                        { color: "#30D158" },
                                        { color: "#FF9F0A" }
                                    ]

                                    delegate: Item {
                                        width: 26; height: 26

                                        Rectangle {
                                            anchors.centerIn: parent
                                            width: index === parent.parent.selected ? 26 : 22
                                            height: index === parent.parent.selected ? 26 : 22
                                            radius: width / 2; color: modelData.color
                                            border.color: index === parent.parent.selected ? "#FFFFFF" : "transparent"
                                            border.width: index === parent.parent.selected ? 2 : 0
                                        }

                                        Rectangle {
                                            visible: index === parent.parent.selected
                                            anchors.centerIn: parent
                                            width: 34; height: 34; radius: 17
                                            color: "transparent"; border.color: modelData.color
                                            border.width: 2; opacity: 0.55
                                        }

                                        MouseArea {
                                            anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                                            onClicked: parent.parent.selected = index
                                        }
                                    }
                                }
                            }
                        }
                    }

                    Rectangle { Layout.fillWidth: true; height: 1; color: Theme.line; opacity: 0.7; Layout.leftMargin: Theme.sp2 }

                    // Tema — segmented sun/moon toggle
                    Item {
                        height: 56; Layout.fillWidth: true

                        RowLayout {
                            anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                            spacing: Theme.sp2

                            Rectangle {
                                width: 34; height: 34; radius: Theme.rSm
                                color: Theme.alpha(Theme.surface2, 0.9)

                                Image {
                                    anchors.centerIn: parent; width: 16; height: 16
                                    source: Theme.dimIcon("icons/moon-dim.svg")
                                    fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                                }
                            }

                            Text {
                                text: "Tema"; color: Theme.ink; font.family: Theme.font
                                font.pixelSize: Theme.tsBody; font.weight: Font.Medium; Layout.fillWidth: true
                            }

                            // Segmented control: Claro | Oscuro
                            Rectangle {
                                height: 30; radius: Theme.rSm
                                color: Theme.card2
                                border.color: Theme.line; border.width: 1
                                implicitWidth: segRow.width + 4

                                Row {
                                    id: segRow
                                    anchors.centerIn: parent
                                    spacing: 0

                                    // Claro segment
                                    Rectangle {
                                        width: 68; height: 26; radius: Theme.rSm - 2
                                        color: Theme.mode === "light"
                                               ? Theme.alpha(Theme.accent, 0.18)
                                               : "transparent"
                                        border.color: Theme.mode === "light"
                                                      ? Theme.alpha(Theme.accentBright, 0.35)
                                                      : "transparent"
                                        border.width: 1
                                        Behavior on color { ColorAnimation { duration: 160 } }

                                        Text {
                                            anchors.centerIn: parent
                                            text: "Claro"
                                            color: Theme.mode === "light" ? Theme.ink : Theme.ink3
                                            font.family: Theme.font
                                            font.pixelSize: Theme.tsCaption
                                            font.weight: Theme.mode === "light" ? Font.Medium : Font.Normal
                                        }

                                        MouseArea {
                                            anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                                            onClicked: Theme.mode = "light"
                                        }
                                    }

                                    // Oscuro segment
                                    Rectangle {
                                        width: 68; height: 26; radius: Theme.rSm - 2
                                        color: Theme.mode === "dark"
                                               ? Theme.alpha(Theme.accent, 0.18)
                                               : "transparent"
                                        border.color: Theme.mode === "dark"
                                                      ? Theme.alpha(Theme.accentBright, 0.35)
                                                      : "transparent"
                                        border.width: 1
                                        Behavior on color { ColorAnimation { duration: 160 } }

                                        Text {
                                            anchors.centerIn: parent
                                            text: "Oscuro"
                                            color: Theme.mode === "dark" ? Theme.ink : Theme.ink3
                                            font.family: Theme.font
                                            font.pixelSize: Theme.tsCaption
                                            font.weight: Theme.mode === "dark" ? Font.Medium : Font.Normal
                                        }

                                        MouseArea {
                                            anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                                            onClicked: Theme.mode = "dark"
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

            Item { height: Theme.sp3; width: 1 }

            // ── Usuario ────────────────────────────────────────────────────────
            Text {
                text: "Usuario"
                color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro; font.weight: Font.Medium
                Layout.leftMargin: 6; Layout.bottomMargin: Theme.sp1
            }

            Item {
                Layout.fillWidth: true
                height: userCard.height

                Rectangle {
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    anchors.topMargin: 3; anchors.leftMargin: 2; anchors.rightMargin: -2
                    height: parent.height; radius: Theme.rLg
                    color: "#000000"
                    opacity: Theme.mode === "light" ? 0.06 : 0.16
                }

                Rectangle {
                    id: userCard
                    width: parent.width; height: userRow.implicitHeight + Theme.sp4
                    radius: Theme.rLg; color: Theme.card; border.color: Theme.line; border.width: 1

                    Rectangle {
                        anchors { top: parent.top; left: parent.left; right: parent.right }
                        anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                        height: 1; radius: Theme.rLg - 1
                        color: Theme.mode === "light" ? "#000000" : "#FFFFFF"
                        opacity: Theme.mode === "light" ? 0.03 : 0.06
                    }

                    RowLayout {
                        id: userRow
                        anchors {
                            left: parent.left; right: parent.right; top: parent.top
                            leftMargin: Theme.sp2; rightMargin: Theme.sp2; topMargin: Theme.sp2
                        }
                        spacing: Theme.sp2

                        Rectangle {
                            width: 44; height: 44; radius: 22; color: Theme.accent

                            Image {
                                anchors.centerIn: parent; width: 22; height: 22
                                source: "icons/user.svg"
                                fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                            }
                        }

                        ColumnLayout {
                            spacing: 3; Layout.fillWidth: true
                            Text { text: "luiscorrea-dev"; color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsBody; font.weight: Font.DemiBold }
                            Text { text: "Administrador del sistema"; color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption }
                        }

                        Image {
                            width: 14; height: 14; source: Theme.dimIcon("icons/chevron-right-dim.svg")
                            fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                        }
                    }
                }
            }

            Item { height: Theme.sp3; width: 1 }

            // ── Privacidad y seguridad ─────────────────────────────────────────
            Text {
                text: "Privacidad y seguridad"
                color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro; font.weight: Font.Medium
                Layout.leftMargin: 6; Layout.bottomMargin: Theme.sp1
            }

            Item {
                Layout.fillWidth: true
                height: privacidadCol.height

                Rectangle {
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    anchors.topMargin: 3; anchors.leftMargin: 2; anchors.rightMargin: -2
                    height: parent.height; radius: Theme.rLg
                    color: "#000000"
                    opacity: Theme.mode === "light" ? 0.06 : 0.16
                }

                Rectangle {
                    anchors.fill: parent; radius: Theme.rLg; color: Theme.card
                    border.color: Theme.line; border.width: 1

                    Rectangle {
                        anchors { top: parent.top; left: parent.left; right: parent.right }
                        anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                        height: 1; radius: Theme.rLg - 1
                        color: Theme.mode === "light" ? "#000000" : "#FFFFFF"
                        opacity: Theme.mode === "light" ? 0.03 : 0.06
                    }
                }

                ColumnLayout {
                    id: privacidadCol
                    anchors { left: parent.left; right: parent.right }
                    spacing: 0

                    // Permiso row — with two-line description + toggle
                    Item {
                        height: 62; Layout.fillWidth: true

                        RowLayout {
                            anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                            spacing: Theme.sp2

                            Rectangle {
                                width: 34; height: 34; radius: Theme.rSm
                                color: Theme.alpha(Theme.ok, 0.10)

                                Image {
                                    anchors.centerIn: parent; width: 16; height: 16
                                    source: "icons/shield-check-ok.svg"
                                    fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                                }
                            }

                            ColumnLayout {
                                spacing: 2; Layout.fillWidth: true
                                Text { text: "Nada sale sin tu permiso"; color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsBody; font.weight: Font.Medium }
                                Text { text: "Lumen pide confirmación antes de cualquier acción sensible"; color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption }
                            }

                            Rectangle {
                                id: permToggle
                                property bool isOn: true
                                width: 44; height: 24; radius: 12
                                color: isOn ? Theme.ok : Theme.ink4
                                Behavior on color { ColorAnimation { duration: 160 } }

                                Rectangle {
                                    width: 18; height: 18; radius: 9; color: "#FFFFFF"
                                    anchors { verticalCenter: parent.verticalCenter; left: parent.left; leftMargin: permToggle.isOn ? 22 : 3 }
                                    Behavior on anchors.leftMargin { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                                }
                                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: permToggle.isOn = !permToggle.isOn }
                            }
                        }
                    }

                    Rectangle { Layout.fillWidth: true; height: 1; color: Theme.line; opacity: 0.7; Layout.leftMargin: Theme.sp2 }

                    // Ver capas row — active badge + chevron
                    Item {
                        height: 56; Layout.fillWidth: true

                        RowLayout {
                            anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                            spacing: Theme.sp2

                            Rectangle {
                                width: 34; height: 34; radius: Theme.rSm
                                color: Theme.alpha(Theme.surface2, 0.9)

                                Image {
                                    anchors.centerIn: parent; width: 16; height: 16
                                    source: Theme.dimIcon("icons/lock-dim.svg")
                                    fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                                }
                            }

                            Text {
                                text: "Ver capas de seguridad"; color: Theme.ink; font.family: Theme.font
                                font.pixelSize: Theme.tsBody; font.weight: Font.Medium; Layout.fillWidth: true
                            }

                            // Active badge — Lucide check + count
                            Rectangle {
                                height: 22; radius: Theme.rSm - 2
                                color: Theme.alpha(Theme.ok, 0.10)
                                border.color: Theme.alpha(Theme.ok, 0.20); border.width: 1
                                implicitWidth: secBadgeRow.implicitWidth + 14

                                RowLayout {
                                    id: secBadgeRow; anchors.centerIn: parent; spacing: 4

                                    Image {
                                        width: 10; height: 10; source: "icons/check-ok.svg"
                                        fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                                    }
                                    Text {
                                        text: "6 activas"; color: Theme.ok
                                        font.family: Theme.font; font.pixelSize: Theme.tsCaption; font.weight: Font.Medium
                                    }
                                }
                            }

                            Image {
                                width: 14; height: 14; source: Theme.dimIcon("icons/chevron-right-dim.svg")
                                fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                            }
                        }

                        MouseArea {
                            anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                            onClicked: if (settingsView.shell) settingsView.shell.go(6)
                        }
                    }
                }
            }

            Item { height: Theme.sp3; width: 1 }

            // ── Asistente ──────────────────────────────────────────────────────
            Text {
                text: "Asistente"
                color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro; font.weight: Font.Medium
                Layout.leftMargin: 6; Layout.bottomMargin: Theme.sp1
            }

            Item {
                Layout.fillWidth: true
                height: asistCol.height

                Rectangle {
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    anchors.topMargin: 3; anchors.leftMargin: 2; anchors.rightMargin: -2
                    height: parent.height; radius: Theme.rLg
                    color: "#000000"
                    opacity: Theme.mode === "light" ? 0.06 : 0.16
                }

                Rectangle {
                    anchors.fill: parent; radius: Theme.rLg; color: Theme.card
                    border.color: Theme.line; border.width: 1

                    Rectangle {
                        anchors { top: parent.top; left: parent.left; right: parent.right }
                        anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                        height: 1; radius: Theme.rLg - 1
                        color: Theme.mode === "light" ? "#000000" : "#FFFFFF"
                        opacity: Theme.mode === "light" ? 0.03 : 0.06
                    }
                }

                ColumnLayout {
                    id: asistCol
                    anchors { left: parent.left; right: parent.right }
                    spacing: 0

                    // Voz
                    Item {
                        height: 56; Layout.fillWidth: true

                        RowLayout {
                            anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                            spacing: Theme.sp2

                            Rectangle {
                                width: 34; height: 34; radius: Theme.rSm
                                color: Theme.alpha(Theme.surface2, 0.9)

                                Image {
                                    anchors.centerIn: parent; width: 16; height: 16
                                    source: Theme.dimIcon("icons/mic-dim.svg")
                                    fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                                }
                            }

                            Text {
                                text: "Voz"; color: Theme.ink; font.family: Theme.font
                                font.pixelSize: Theme.tsBody; font.weight: Font.Medium; Layout.fillWidth: true
                            }

                            Rectangle {
                                id: vozToggle
                                property bool isOn: true
                                width: 44; height: 24; radius: 12
                                color: isOn ? Theme.accent : Theme.ink4
                                Behavior on color { ColorAnimation { duration: 160 } }

                                Rectangle {
                                    width: 18; height: 18; radius: 9; color: "#FFFFFF"
                                    anchors { verticalCenter: parent.verticalCenter; left: parent.left; leftMargin: vozToggle.isOn ? 22 : 3 }
                                    Behavior on anchors.leftMargin { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                                }
                                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: vozToggle.isOn = !vozToggle.isOn }
                            }
                        }
                    }

                    Rectangle { Layout.fillWidth: true; height: 1; color: Theme.line; opacity: 0.7; Layout.leftMargin: Theme.sp2 }

                    // Sugerencias proactivas
                    Item {
                        height: 62; Layout.fillWidth: true

                        RowLayout {
                            anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                            spacing: Theme.sp2

                            Rectangle {
                                width: 34; height: 34; radius: Theme.rSm
                                color: Theme.alpha(Theme.accent, 0.14)

                                Image {
                                    anchors.centerIn: parent; width: 16; height: 16
                                    source: Theme.accentIcon("icons/sparkles-accent.svg")
                                    fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                                }
                            }

                            ColumnLayout {
                                spacing: 2; Layout.fillWidth: true
                                Text { text: "Sugerencias proactivas"; color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsBody; font.weight: Font.Medium }
                                Text { text: "Lumen anticipa lo que podrías necesitar"; color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption }
                            }

                            Rectangle {
                                id: sugToggle
                                property bool isOn: true
                                width: 44; height: 24; radius: 12
                                color: isOn ? Theme.accent : Theme.ink4
                                Behavior on color { ColorAnimation { duration: 160 } }

                                Rectangle {
                                    width: 18; height: 18; radius: 9; color: "#FFFFFF"
                                    anchors { verticalCenter: parent.verticalCenter; left: parent.left; leftMargin: sugToggle.isOn ? 22 : 3 }
                                    Behavior on anchors.leftMargin { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                                }
                                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: sugToggle.isOn = !sugToggle.isOn }
                            }
                        }
                    }
                }
            }

            Item { height: Theme.sp3; width: 1 }

            // ── Acerca de ──────────────────────────────────────────────────────
            Text {
                text: "Acerca de"
                color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsMicro; font.weight: Font.Medium
                Layout.leftMargin: 6; Layout.bottomMargin: Theme.sp1
            }

            Item {
                Layout.fillWidth: true
                height: aboutCard.height

                Rectangle {
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    anchors.topMargin: 3; anchors.leftMargin: 2; anchors.rightMargin: -2
                    height: parent.height; radius: Theme.rLg
                    color: "#000000"
                    opacity: Theme.mode === "light" ? 0.06 : 0.16
                }

                Rectangle {
                    id: aboutCard
                    width: parent.width; height: aboutRow.implicitHeight + Theme.sp4
                    radius: Theme.rLg; color: Theme.card; border.color: Theme.line; border.width: 1

                    Rectangle {
                        anchors { top: parent.top; left: parent.left; right: parent.right }
                        anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                        height: 1; radius: Theme.rLg - 1
                        color: Theme.mode === "light" ? "#000000" : "#FFFFFF"
                        opacity: Theme.mode === "light" ? 0.03 : 0.06
                    }

                    RowLayout {
                        id: aboutRow
                        anchors {
                            left: parent.left; right: parent.right; top: parent.top
                            leftMargin: Theme.sp2; rightMargin: Theme.sp2; topMargin: Theme.sp2
                        }
                        spacing: Theme.sp2

                        Rectangle {
                            width: 44; height: 44; radius: Theme.rMd; color: Theme.accent

                            Image {
                                anchors.centerIn: parent; width: 24; height: 24
                                source: Theme.mode === "light" ? "icons/lumen-mark-light.svg" : "icons/lumen-mark.svg"
                                fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                            }
                        }

                        ColumnLayout {
                            spacing: 4; Layout.fillWidth: true
                            Text { text: "Lumen 1.0"; color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsBody; font.weight: Font.DemiBold }
                            Text { text: "La distribución de Linux más agéntica del mundo."; color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption }
                        }

                        Image {
                            width: 14; height: 14; source: Theme.dimIcon("icons/chevron-right-dim.svg")
                            fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                        }
                    }
                }
            }

            Item { height: Theme.sp3; width: 1 }
        }
    }
}
