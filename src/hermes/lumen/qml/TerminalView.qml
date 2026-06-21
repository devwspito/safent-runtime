import QtQuick
import QtQuick.Layouts
import "."

// Lumen — Terminal REAL. Ejecuta comandos de verdad en el equipo del operador
// (backend.runShell → /bin/bash). Cero salida canned.
// No emoji. No MultiEffect. No RotationAnimator/loops:Infinite.
Item {
    id: terminalView
    property var shell: null

    property string cwd: "~"
    function _shortCwd(p) {
        if (!p) return "~"
        var home = "/var/home/" // fallback; el backend manda la ruta real
        var parts = p.split("/").filter(function (x) { return x.length > 0 })
        return parts.length ? parts[parts.length - 1] : "/"
    }

    // Historial REAL de comandos ejecutados.
    ListModel { id: history }

    Connections {
        target: backend
        function onShellOutput(cmd, output, newCwd) {
            terminalView.cwd = terminalView._shortCwd(newCwd)
            if (cmd === "__clear__") { history.clear(); return }
            history.append({ cmd: cmd, output: output })
            Qt.callLater(function () {
                termFlick.contentY = Math.max(0, termContent.height - termFlick.height)
            })
        }
    }

    // ── outer card ─────────────────────────────────────────────────────────
    Rectangle {
        anchors { fill: parent; margins: Theme.sp2 }
        radius: Theme.rLg
        color: "#0A090E"
        border.color: Theme.line; border.width: 1
        clip: true

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            // ── title bar ──────────────────────────────────────────────────
            Rectangle {
                Layout.fillWidth: true; height: 42
                color: "#0D0C12"
                radius: Theme.rLg

                Rectangle {
                    anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
                    height: 1; color: Theme.line
                }

                RowLayout {
                    anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                    spacing: 0

                    Row {
                        spacing: 7
                        Repeater {
                            model: ["#FF5F57", "#FFBD2E", "#28C840"]
                            Rectangle {
                                width: 11; height: 11; radius: 6
                                color: modelData
                                opacity: 0.88
                            }
                        }
                    }

                    Item { Layout.fillWidth: true }

                    RowLayout {
                        spacing: 7
                        Image {
                            width: 13; height: 13
                            source: Theme.dimIcon("icons/terminal-dim.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                        Text {
                            text: "Terminal"
                            color: Theme.ink3
                            font.family: Theme.font
                            font.pixelSize: Theme.tsCaption
                            font.weight: Font.Medium
                        }
                    }

                    Item { Layout.fillWidth: true }
                    Item { width: 44 }
                }
            }

            // ── terminal surface (REAL) ────────────────────────────────────
            Flickable {
                id: termFlick
                Layout.fillWidth: true; Layout.fillHeight: true
                contentWidth: termContent.width
                contentHeight: termContent.height
                clip: true
                interactive: true
                boundsBehavior: Flickable.StopAtBounds

                WheelScroll { target: termFlick }

                Item {
                    id: termContent
                    width: termFlick.width
                    height: termColumn.height + Theme.sp4

                    ColumnLayout {
                        id: termColumn
                        anchors {
                            top: parent.top; left: parent.left; right: parent.right
                            topMargin: Theme.sp3; leftMargin: Theme.sp3; rightMargin: Theme.sp3
                        }
                        spacing: 4

                        // Línea de bienvenida real (sin stats inventadas).
                        Text {
                            text: "Terminal de Lumen — escribe un comando y pulsa Enter. 'clear' limpia, 'cd' navega."
                            color: Theme.ink4
                            font.family: Theme.mono
                            font.pixelSize: Theme.tsMicro
                            Layout.bottomMargin: Theme.sp1
                        }

                        // Historial REAL.
                        Repeater {
                            model: history

                            delegate: ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2

                                RowLayout {
                                    spacing: 6
                                    Image {
                                        width: 12; height: 12
                                        source: Theme.accentIcon("icons/chevron-right-accent.svg")
                                        fillMode: Image.PreserveAspectFit
                                        smooth: true; mipmap: true
                                    }
                                    Text {
                                        text: model.cmd
                                        color: "#B8A8F0"
                                        font.family: Theme.mono
                                        font.pixelSize: Theme.tsCaption
                                        textFormat: Text.PlainText
                                    }
                                }

                                Text {
                                    visible: model.output.length > 0
                                    text: model.output
                                    color: Theme.ink2
                                    font.family: Theme.mono
                                    font.pixelSize: Theme.tsMicro
                                    textFormat: Text.PlainText
                                    wrapMode: Text.WrapAnywhere
                                    Layout.fillWidth: true
                                    Layout.bottomMargin: 4
                                }
                            }
                        }

                        // Prompt activo REAL con entrada de texto.
                        RowLayout {
                            spacing: 6
                            Layout.fillWidth: true
                            Layout.topMargin: 2

                            Image {
                                width: 12; height: 12
                                source: Theme.accentIcon("icons/chevron-right-accent.svg")
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                            }

                            Text {
                                text: "luis@lumen:" + terminalView.cwd + "$"
                                color: "#B8A8F0"
                                font.family: Theme.mono
                                font.pixelSize: Theme.tsCaption
                                font.weight: Font.Medium
                            }

                            TextInput {
                                id: cmdInput
                                Layout.fillWidth: true
                                color: Theme.ink
                                font.family: Theme.mono
                                font.pixelSize: Theme.tsCaption
                                selectionColor: Theme.alpha(Theme.accent, 0.4)
                                focus: true
                                clip: true
                                onAccepted: {
                                    var c = text
                                    text = ""
                                    backend.runShell(c)
                                }
                            }
                        }

                        Item { height: Theme.sp3; width: 1 }
                    }
                }

                // Mantener el foco en la entrada al hacer clic en cualquier sitio.
                MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.LeftButton
                    onClicked: cmdInput.forceActiveFocus()
                    z: -1
                }
            }

            // ── status bar ─────────────────────────────────────────────────
            Rectangle {
                Layout.fillWidth: true; height: 28
                color: "#0D0C12"

                Rectangle {
                    anchors { top: parent.top; left: parent.left; right: parent.right }
                    height: 1; color: Theme.line
                }

                RowLayout {
                    anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                    spacing: Theme.sp2

                    Row {
                        spacing: 6
                        Rectangle {
                            width: 6; height: 6; radius: 3
                            color: Theme.ok
                            anchors.verticalCenter: parent.verticalCenter
                        }
                        Text {
                            text: "bash · utf-8"
                            color: Theme.ink3
                            font.family: Theme.mono
                            font.pixelSize: Theme.tsMicro
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }

                    Item { Layout.fillWidth: true }

                    Text {
                        text: backend.clock
                        color: Theme.ink4
                        font.family: Theme.mono
                        font.pixelSize: Theme.tsMicro
                    }
                }
            }
        }
    }
}
