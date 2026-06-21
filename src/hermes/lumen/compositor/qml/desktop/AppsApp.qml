import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

Rectangle {
    id: appsApp
    anchors.fill: parent
    color: "transparent"

    property var extensions: []

    Component.onCompleted: loadExtensions()

    Flickable {
        id: appsFlick
        anchors.fill: parent
        anchors.margins: Math.round(Tokens.spXl * root.sf)
        contentHeight: appsCol.height
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: LumenScrollBar { sf: root.sf; policy: ScrollBar.AsNeeded }

        WheelHandler {
            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            onWheel: (event) => {
                var f = appsFlick;
                f.contentY = Math.max(0, Math.min(Math.max(0, f.contentHeight - f.height), f.contentY - event.angleDelta.y));
            }
        }

        ColumnLayout {
            id: appsCol
            width: parent.width
            spacing: Math.round(18 * root.sf)

            // Extensions grid — responsive columns based on available width
            GridLayout {
                Layout.fillWidth: true
                columns: Math.max(1, Math.floor(appsFlick.width / Math.round(220 * root.sf)))
                columnSpacing: Math.round(Tokens.spLg * root.sf)
                rowSpacing: Math.round(Tokens.spLg * root.sf)

                Repeater {
                    model: extensions

                    delegate: Rectangle {
                        Layout.fillWidth: true
                        Layout.minimumWidth: Math.round(200 * root.sf)
                        height: extCol.height + Math.round(Tokens.spXl * root.sf)
                        radius: Math.round(Tokens.radiusMd * root.sf)
                        color: Tokens.bgCard
                        border.color: Tokens.borderSubtle
                        border.width: 1
                        property string extName: modelData.name || ""

                        ColumnLayout {
                            id: extCol
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: Math.round(Tokens.spMd * root.sf)
                            spacing: Math.round(Tokens.spSm * root.sf)

                            RowLayout {
                                spacing: Math.round(Tokens.spSm * root.sf)

                                Text {
                                    text: ""
                                    font.pixelSize: Math.round(18 * root.sf)
                                    font.family: root.iconFont
                                    font.weight: Font.Black
                                    color: Tokens.textMuted
                                }

                                ColumnLayout {
                                    spacing: Math.round(2 * root.sf)
                                    Text {
                                        text: modelData.name || "Extension"
                                        font.pixelSize: Math.round(13 * root.sf)
                                        font.family: Tokens.fontBody
                                        font.weight: Font.Medium
                                        color: Tokens.textPrimary
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                    }
                                    Text {
                                        text: modelData.schedule || "Manual"
                                        font.pixelSize: Math.round(10 * root.sf)
                                        font.family: Tokens.fontBody
                                        color: Tokens.textMuted
                                    }
                                }

                                Item { Layout.fillWidth: true }

                                LumenChip {
                                    sf: root.sf
                                    text: modelData.enabled ? "On" : "Off"
                                    tone: modelData.enabled ? "success" : "neutral"
                                }
                            }

                            Text {
                                Layout.fillWidth: true
                                text: modelData.description || "No description"
                                font.pixelSize: Math.round(12 * root.sf)
                                font.family: Tokens.fontBody
                                color: Tokens.textSecondary
                                wrapMode: Text.WordWrap
                                maximumLineCount: 2
                                elide: Text.ElideRight
                            }

                            // Actions
                            RowLayout {
                                spacing: Math.round(Tokens.spXs * root.sf)

                                LumenButton {
                                    sf: root.sf
                                    label: "Run"
                                    variant: "primary"
                                    implicitWidth: Math.round(64 * root.sf)
                                    implicitHeight: Math.round(28 * root.sf)
                                    onClicked: runExt(extName)
                                }
                                LumenButton {
                                    sf: root.sf
                                    label: "Toggle"
                                    variant: "secondary"
                                    implicitWidth: Math.round(64 * root.sf)
                                    implicitHeight: Math.round(28 * root.sf)
                                    onClicked: toggleExt(extName)
                                }
                                LumenButton {
                                    sf: root.sf
                                    label: "Delete"
                                    variant: "danger"
                                    implicitWidth: Math.round(64 * root.sf)
                                    implicitHeight: Math.round(28 * root.sf)
                                    onClicked: deleteExt(extName)
                                }
                                LumenButton {
                                    sf: root.sf
                                    label: "Edit…"
                                    variant: "ghost"
                                    implicitWidth: Math.round(64 * root.sf)
                                    implicitHeight: Math.round(28 * root.sf)
                                    enabled: false
                                }
                            }
                        }
                    }
                }
            }

            // Empty state
            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.alignment: Qt.AlignHCenter | Qt.AlignVCenter
                spacing: Math.round(Tokens.spSm * root.sf)
                visible: extensions.length === 0

                Text {
                    Layout.alignment: Qt.AlignHCenter
                    text: ""
                    font.pixelSize: Math.round(40 * root.sf)
                    font.family: root.iconFont
                    font.weight: Font.Black
                    color: Tokens.textMuted
                }
                Text {
                    Layout.alignment: Qt.AlignHCenter
                    text: "No extensions installed yet"
                    font.pixelSize: Math.round(14 * root.sf)
                    font.family: Tokens.fontDisplay
                    font.weight: Font.Medium
                    color: Tokens.textSecondary
                }
                Text {
                    Layout.alignment: Qt.AlignHCenter
                    text: "Ask Hermes to create extensions for you"
                    font.pixelSize: Math.round(12 * root.sf)
                    font.family: Tokens.fontBody
                    color: Tokens.textMuted
                }
            }
        }
    }

    function loadExtensions() {
        // Las extensiones no tienen backend D-Bus en esta versión.
        // La lista permanece vacía — el empty state ya muestra el mensaje honesto.
        extensions = [];
    }

    function runExt(name) {
        root.showToast("Extensiones: próximamente", "info");
    }

    function toggleExt(name) {
        root.showToast("Extensiones: próximamente", "info");
    }

    function deleteExt(name) {
        root.showToast("Extensiones: próximamente", "info");
    }
}
