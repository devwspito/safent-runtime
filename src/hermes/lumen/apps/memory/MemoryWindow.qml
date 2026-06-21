import QtQuick
import QtQuick.Layouts
import QtQuick.Window
import QtQuick.Controls
import "../../qml"

// MemoryWindow — standalone capability app: Memoria del agente.
//
// Phase-0 visual refresh:
//   - All hardcoded hex/Qt.rgba() replaced by Theme tokens.
//   - Indigo icon tile (#6E56CF / #8C73F0) → Theme.accent blue.
//   - Content clamped to Math.min(width-80, 760) for readable column.
//   - Text secondaries ≥ Theme.ink3 (~7:1). Tags token: Theme.accentBright.
//   - Timestamp was "#6E6E76" (3.2:1 fail) → Theme.ink3.
//   - Search field focus ring uses Theme.alpha(Theme.accentBright, 0.55).
//   - Gradient bg via Theme.bgBottom.
//
// Data source:
//   ListMemory(limit)  → JSON   (T047 dependency; polled every 15 s)
//   SearchMemory(q)    → JSON   (T047 dependency; on-demand search)
//
// HONEST UNAVAILABLE STATE:
//   Both verbs are declared dependencies (T047 backend task). Until the
//   daemon exposes them the view shows:
//     "Función no disponible — ListMemory/SearchMemory no están implementados
//      en el daemon aún (T047)."
//   NEVER a mock list. NEVER invented memory entries.
//
// No mutations. No effectors. No broker. No HTTP.
//
// Context properties:
//   backend    — MemoryBackend (listLoaded, memoryUnavailable property)
//   qmlBaseDir — absolute path to lumen/qml/

Window {
    id: appWindow
    title: "Memoria — Hermes"
    minimumWidth: 720; minimumHeight: 480
    width: 960; height: 640
    visible: true
    color: Theme.bg0

    // Gradient bg — free on software/VNC render, adds depth.
    Rectangle {
        anchors.fill: parent; z: -1
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.bg0 }
            GradientStop { position: 1.0; color: Theme.bgBottom }
        }
    }

    // ── Memory list model ────────────────────────────────────────────────
    ListModel { id: memoryModel }
    property bool memoryUnavailable: typeof backend.memoryUnavailable !== "undefined"
                                     ? backend.memoryUnavailable : false
    property string searchQuery: ""

    function _populate(json) {
        var arr = []
        try { arr = JSON.parse(json) } catch (e) { arr = [] }
        memoryModel.clear()
        for (var i = 0; i < arr.length; i++) {
            var m = arr[i]
            memoryModel.append({
                memId:    m.id || m.memory_id || "",
                content:  m.content || m.text || m.summary || "",
                tags:     Array.isArray(m.tags) ? m.tags.join(", ") : (m.tags || ""),
                created:  m.created_at || m.timestamp || ""
            })
        }
    }

    Connections {
        target: backend
        function onListLoaded(key, json) {
            if (key === "memory" || key === "memory_search") appWindow._populate(json)
        }
    }

    // ── Title bar ────────────────────────────────────────────────────────
    Rectangle {
        id: titleBar
        anchors { top: parent.top; left: parent.left; right: parent.right }
        height: 56; color: Theme.surface

        Rectangle {
            anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
            height: 1; color: Theme.line
        }

        RowLayout {
            anchors { fill: parent; leftMargin: Theme.sp3; rightMargin: Theme.sp3 }
            spacing: Theme.sp2

            Rectangle {
                width: 32; height: 32; radius: Theme.rSm
                color: Theme.alpha(Theme.accent, 0.14)
                border.color: Theme.alpha(Theme.accentBright, 0.18); border.width: 1
                Image {
                    anchors.centerIn: parent; width: 16; height: 16
                    source: Qt.resolvedUrl("file://" + qmlBaseDir + "/icons/bookmark-dim.svg")
                    fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                }
            }

            ColumnLayout {
                spacing: 2
                Text {
                    text: "Memoria"
                    color: Theme.ink
                    font.family: Theme.font; font.pixelSize: Theme.tsSubtitle; font.weight: Font.DemiBold
                }
                Text {
                    text: "Lo que Lumen recuerda"
                    color: Theme.ink3
                    font.family: Theme.font; font.pixelSize: Theme.tsCaption
                }
            }

            Item { Layout.fillWidth: true }

            Rectangle {
                height: 24; radius: Theme.rSm - 2
                implicitWidth: connRow.implicitWidth + 16
                color: backend.connected ? Theme.alpha(Theme.ok, 0.10) : Theme.alpha(Theme.warn, 0.10)
                border.color: backend.connected ? Theme.alpha(Theme.ok, 0.22) : Theme.alpha(Theme.warn, 0.22)
                border.width: 1
                Row {
                    id: connRow; anchors.centerIn: parent; spacing: 5
                    Rectangle { width: 6; height: 6; radius: 3; color: backend.connected ? Theme.ok : Theme.warn }
                    Text {
                        text: backend.connected ? "Lumen activo" : "Lumen no responde"
                        color: backend.connected ? Theme.ok : Theme.warn
                        font.family: Theme.font; font.pixelSize: Theme.tsMicro; font.weight: Font.Medium
                    }
                }
            }
        }
    }

    // ── Content ──────────────────────────────────────────────────────────
    Item {
        anchors { top: titleBar.bottom; left: parent.left; right: parent.right; bottom: parent.bottom }

        // Honest "not available yet" state — T047 dependency not met
        Item {
            anchors.fill: parent
            visible: !backend.loading && !backend.daemonError.length && appWindow.memoryUnavailable

            Column {
                anchors.centerIn: parent; spacing: Theme.sp2; width: Math.min(parent.width - 64, 480)
                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: 64; height: 64; radius: 32
                    color: Theme.alpha(Theme.ink4, 0.18)
                    border.color: Theme.line; border.width: 1
                    Image {
                        anchors.centerIn: parent; width: 28; height: 28
                        source: Qt.resolvedUrl("file://" + qmlBaseDir + "/icons/bookmark-dim.svg")
                        fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                    }
                }
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Esta función llegará pronto"
                    color: Theme.ink
                    font.family: Theme.font; font.pixelSize: Theme.tsLead; font.weight: Font.DemiBold
                }
                Text {
                    width: parent.width; horizontalAlignment: Text.AlignHCenter
                    text: "Aquí verás todo lo que Lumen ha aprendido y recordado durante sus tareas. Estamos preparándolo."
                    color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsBody
                    wrapMode: Text.WordWrap; lineHeight: 1.5
                }
            }
        }

        // Loading state
        Item {
            anchors.fill: parent
            visible: backend.loading && !backend.daemonError.length
            Column {
                anchors.centerIn: parent; spacing: Theme.sp2
                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: 48; height: 48; radius: 24
                    color: Theme.alpha(Theme.accent, 0.10)
                    border.color: Theme.alpha(Theme.accentBright, 0.22); border.width: 1
                    Image {
                        anchors.centerIn: parent; width: 22; height: 22
                        source: Qt.resolvedUrl("file://" + qmlBaseDir + "/icons/rotate-cw-dim.svg")
                        fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true; opacity: 0.8
                    }
                }
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Cargando memoria…"
                    color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsBody
                }
            }
        }

        // Error state
        Item {
            anchors.fill: parent
            visible: backend.daemonError.length > 0
            Column {
                anchors.centerIn: parent; spacing: Theme.sp2; width: Math.min(parent.width - 64, 420)
                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: 56; height: 56; radius: 28
                    color: Theme.alpha(Theme.warn, 0.10)
                    border.color: Theme.alpha(Theme.warn, 0.24); border.width: 1
                    Image {
                        anchors.centerIn: parent; width: 24; height: 24
                        source: Qt.resolvedUrl("file://" + qmlBaseDir + "/icons/alert-circle-warn.svg")
                        fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true
                    }
                }
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Lumen no responde"
                    color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsBody; font.weight: Font.DemiBold
                }
                Text {
                    width: parent.width; horizontalAlignment: Text.AlignHCenter
                    text: backend.daemonError; color: Theme.ink3
                    font.family: Theme.font; font.pixelSize: Theme.tsCaption; wrapMode: Text.WordWrap
                }
            }
        }

        // Real content (when T047 is implemented and data is available)
        ColumnLayout {
            anchors.fill: parent
            spacing: 0
            visible: !backend.loading && !backend.daemonError.length && !appWindow.memoryUnavailable

            // Search bar — clamped to readable column width
            Item {
                Layout.fillWidth: true; height: 56

                Item {
                    anchors.verticalCenter: parent.verticalCenter
                    width: Math.min(parent.width - 80, 760)
                    anchors.horizontalCenter: parent.horizontalCenter
                    height: 40

                    Rectangle {
                        anchors.fill: parent
                        radius: Theme.rMd; color: Theme.surface2
                        border.color: searchInput.activeFocus
                            ? Theme.alpha(Theme.accentBright, 0.55)
                            : Theme.line
                        border.width: 1

                        RowLayout {
                            anchors { fill: parent; leftMargin: 12; rightMargin: 12 }
                            spacing: Theme.sp1

                            Image {
                                width: 14; height: 14
                                source: Qt.resolvedUrl("file://" + qmlBaseDir + "/icons/search-dim.svg")
                                fillMode: Image.PreserveAspectFit; smooth: true; mipmap: true; opacity: 0.6
                            }

                            TextInput {
                                id: searchInput
                                Layout.fillWidth: true
                                color: Theme.ink
                                font.family: Theme.font; font.pixelSize: Theme.tsLabel
                                onTextChanged: {
                                    if (text.trim().length > 2)
                                        backend.searchMemory(text.trim())
                                    else if (text.trim().length === 0)
                                        backend.loadList("memory", 50)
                                }
                            }

                            Text {
                                visible: searchInput.text.length === 0
                                text: "Buscar en lo que Lumen recuerda…"
                                color: Theme.inkPlaceholder
                                font.family: Theme.font; font.pixelSize: Theme.tsLabel
                            }
                        }
                    }
                }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: Theme.line }

            // Memory list — centred clamped column
            ListView {
                id: memoryList
                Layout.fillWidth: true; Layout.fillHeight: true
                clip: true; spacing: 0
                topMargin: Theme.sp1; bottomMargin: Theme.sp2
                boundsBehavior: Flickable.StopAtBounds
                model: memoryModel

                ScrollBar.vertical: ScrollBar {
                    policy: ScrollBar.AsNeeded
                    contentItem: Rectangle { radius: 2; color: Theme.alpha(Theme.ink3, 0.28) }
                }

                delegate: Item {
                    width: memoryList.width; height: memCard.height + 6

                    // Centred container — readable column width
                    Item {
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: Math.min(parent.width - 80, 760)
                        height: parent.height

                        // Static shadow underlay
                        Rectangle {
                            anchors {
                                left: parent.left; right: parent.right
                                top: parent.top; topMargin: 2
                                leftMargin: 2; rightMargin: -2
                            }
                            height: memCard.height; radius: Theme.rLg
                            color: "#000000"; opacity: Theme.elevRaised.opacity
                        }

                        Rectangle {
                            id: memCard
                            anchors { left: parent.left; right: parent.right }
                            height: inner.height + Theme.sp3; radius: Theme.rLg
                            color: Theme.card; border.color: Theme.line; border.width: 1

                            // Inner top hairline
                            Rectangle {
                                anchors {
                                    top: parent.top; left: parent.left; right: parent.right
                                    topMargin: 1; leftMargin: 1; rightMargin: 1
                                }
                                height: 1; radius: Theme.rLg - 1
                                color: Theme.highlightTopColor; opacity: Theme.highlightTopOpacity
                            }

                            Column {
                                id: inner
                                anchors {
                                    left: parent.left; right: parent.right
                                    top: parent.top; margins: Theme.sp2; topMargin: 14
                                }
                                spacing: 6

                                Text {
                                    width: parent.width
                                    text: model.content
                                    color: Theme.ink
                                    font.family: Theme.font; font.pixelSize: Theme.tsLabel
                                    wrapMode: Text.WordWrap; maximumLineCount: 4; elide: Text.ElideRight
                                }

                                Row {
                                    spacing: 12
                                    // Tags: accentBright (visible against dark card, AA on dark)
                                    Text {
                                        text: model.tags
                                        color: Theme.accentBright
                                        font.family: Theme.font; font.pixelSize: Theme.tsMicro
                                        visible: model.tags.length > 0
                                    }
                                    // Timestamp: was "#6E6E76" (3.2:1 fail) → ink3 (≥7:1 on dark)
                                    Text {
                                        text: model.created
                                        color: Theme.ink3
                                        font.family: Theme.font; font.pixelSize: Theme.tsMicro
                                    }
                                }
                            }
                        }
                    }
                }

                // Empty state
                Item {
                    visible: memoryModel.count === 0
                    width: memoryList.width; height: memoryList.height

                    Column {
                        anchors.centerIn: parent; spacing: Theme.sp1
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "Nada guardado aún"
                            color: Theme.ink; font.family: Theme.font; font.pixelSize: Theme.tsBody
                        }
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "Cuando Lumen complete tareas, aquí quedará constancia."
                            color: Theme.ink3; font.family: Theme.font; font.pixelSize: Theme.tsCaption
                        }
                    }
                }
            }
        }
    }
}
