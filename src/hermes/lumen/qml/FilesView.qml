import QtQuick
import QtQuick.Layouts
import "."

// Lumen — Archivos. File manager with sidebar, breadcrumb, grid/list.
// Design: real Lucide line-icons, neutral dark palette, 8pt grid.
// No emoji. No MultiEffect. No RotationAnimator/loops:Infinite.
Item {
    id: filesView
    property var shell: null

    property int  activeSidebar: 0
    property bool gridMode: true

    // Sidebar entries — Lucide icon paths. Sin contadores falsos.
    property var sidebarItems: [
        { icon: "icons/home-dim.svg",      label: "Inicio",     count: "" },
        { icon: "icons/file-text-dim.svg", label: "Documentos", count: "" },
        { icon: "icons/download-dim.svg",  label: "Descargas",  count: "" },
        { icon: "icons/image-dim.svg",     label: "Imágenes",   count: "" },
        { icon: "icons/music-dim.svg",     label: "Música",     count: "" },
        { icon: "icons/video-dim.svg",     label: "Vídeos",     count: "" }
    ]

    // Ruta REAL actual + breadcrumb derivado. Cero mock.
    property string currentPath: ""
    property var breadcrumb: ["Inicio"]

    // Archivos REALES del filesystem del operador.
    property var files: []

    function _fmtSize(b) {
        if (b < 1024) return b + " B"
        if (b < 1048576) return (b / 1024).toFixed(0) + " KB"
        if (b < 1073741824) return (b / 1048576).toFixed(1) + " MB"
        return (b / 1073741824).toFixed(1) + " GB"
    }
    function _relTime(epoch) {
        var s = Math.max(0, Math.floor(Date.now() / 1000 - epoch))
        if (s < 3600) return "hace " + Math.max(1, Math.floor(s / 60)) + " min"
        if (s < 86400) return "hace " + Math.floor(s / 3600) + " h"
        if (s < 2592000) return "hace " + Math.floor(s / 86400) + " d"
        return "hace " + Math.floor(s / 2592000) + " mes"
    }
    function _iconFor(f) {
        if (f.is_dir) return "icons/folder-dim.svg"
        var n = (f.name || "").toLowerCase()
        if (/\.(png|jpe?g|gif|webp|svg|bmp)$/.test(n)) return "icons/image-dim.svg"
        if (/\.(mp3|m4a|flac|wav|ogg|m3u)$/.test(n)) return "icons/music-dim.svg"
        if (/\.(mp4|mkv|mov|webm|avi)$/.test(n)) return "icons/video-dim.svg"
        if (/\.(txt|md|nota|rtf|doc|docx|odt)$/.test(n)) return "icons/file-text-dim.svg"
        return "icons/file-dim.svg"
    }
    Connections {
        target: backend
        function onFilesLoaded(path, json) {
            var arr = []; try { arr = JSON.parse(json) } catch (e) { arr = [] }
            filesView.currentPath = path
            var mapped = []
            for (var i = 0; i < arr.length; i++) {
                var f = arr[i]
                mapped.push({
                    name: f.name,
                    path: f.path,
                    icon: filesView._iconFor(f),
                    kind: f.is_dir ? "folder" : "file",
                    meta: (f.is_dir ? "—" : filesView._fmtSize(f.size)) + "   " + filesView._relTime(f.mtime)
                })
            }
            filesView.files = mapped
            // Breadcrumb desde la ruta real.
            var parts = path.split("/").filter(function (p) { return p.length > 0 })
            filesView.breadcrumb = ["Inicio"].concat(parts.slice(-2))
        }
    }
    Component.onCompleted: backend.loadFiles("")
    function openDir(p) { backend.loadFiles(p) }

    // ── outer card ─────────────────────────────────────────────────────────
    Rectangle {
        anchors { fill: parent; margins: Theme.sp2 }
        radius: Theme.rLg
        color: Theme.surface
        border.color: Theme.line; border.width: 1
        clip: true

        RowLayout {
            anchors.fill: parent
            spacing: 0

            // ── sidebar ────────────────────────────────────────────────────
            Rectangle {
                Layout.preferredWidth: 180; Layout.fillHeight: true
                color: Theme.bg0

                // Right hairline
                Rectangle {
                    anchors { right: parent.right; top: parent.top; bottom: parent.bottom }
                    width: 1; color: Theme.line
                }

                ColumnLayout {
                    anchors {
                        fill: parent
                        topMargin: Theme.sp3
                        leftMargin: Theme.sp1
                        rightMargin: Theme.sp1
                        bottomMargin: Theme.sp2
                    }
                    spacing: 2

                    // Section label
                    Text {
                        text: "LUGARES"
                        color: Theme.ink4
                        font.family: Theme.font
                        font.pixelSize: Theme.tsMicro
                        font.weight: Font.Medium
                        font.letterSpacing: 1.2
                        bottomPadding: Theme.sp1 - 2
                        leftPadding: Theme.sp1
                    }

                    Repeater {
                        model: filesView.sidebarItems

                        delegate: Item {
                            id: sidebarRow
                            property bool isActive: index === filesView.activeSidebar
                            Layout.fillWidth: true
                            height: 34

                            Rectangle {
                                anchors.fill: parent; radius: Theme.rSm
                                color: sidebarRow.isActive
                                    ? Theme.alpha(Theme.accent, 0.16)
                                    : sidebarHover.containsMouse ? Theme.alpha(Theme.surface2, 0.7) : "transparent"
                                Behavior on color { ColorAnimation { duration: 100 } }
                            }

                            RowLayout {
                                anchors { fill: parent; leftMargin: Theme.sp1; rightMargin: Theme.sp1 }
                                spacing: 8

                                Image {
                                    width: 15; height: 15
                                    source: Theme.dimIcon(modelData.icon)
                                    fillMode: Image.PreserveAspectFit
                                    smooth: true; mipmap: true
                                    opacity: sidebarRow.isActive ? 1.0 : 0.65
                                }

                                Text {
                                    Layout.fillWidth: true
                                    text: modelData.label
                                    color: sidebarRow.isActive ? Theme.ink : Theme.ink2
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsCaption + 1
                                    font.weight: sidebarRow.isActive ? Font.Medium : Font.Normal
                                    elide: Text.ElideRight
                                }

                                // Count badge
                                Rectangle {
                                    visible: modelData.count !== ""
                                    height: 16
                                    implicitWidth: countTxt.width + 8
                                    radius: Theme.rSm - 4
                                    color: Theme.alpha(Theme.card2, 0.9)

                                    Text {
                                        id: countTxt
                                        anchors.centerIn: parent
                                        text: modelData.count
                                        color: Theme.ink4
                                        font.family: Theme.font
                                        font.pixelSize: Theme.tsMicro
                                    }
                                }
                            }

                            MouseArea {
                                id: sidebarHover
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: filesView.activeSidebar = index
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }

                    // Storage bar
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        Layout.leftMargin: Theme.sp1
                        Layout.rightMargin: Theme.sp1
                        Layout.bottomMargin: 4

                        Rectangle {
                            Layout.fillWidth: true; height: 1; color: Theme.line2
                        }

                        Item { height: 2 }

                        Text {
                            text: "Almacenamiento"
                            color: Theme.ink4
                            font.family: Theme.font
                            font.pixelSize: Theme.tsMicro
                        }

                        // Progress track
                        Rectangle {
                            Layout.fillWidth: true; height: 3; radius: 2
                            color: Theme.card2

                            Rectangle {
                                width: parent.width * 0.38; height: parent.height; radius: 2
                                color: Theme.accent
                                opacity: 0.80
                            }
                        }

                        Text {
                            text: "56 GB de 256 GB"
                            color: Theme.ink3
                            font.family: Theme.font
                            font.pixelSize: Theme.tsMicro
                        }
                    }
                }
            }

            // ── main pane ──────────────────────────────────────────────────
            ColumnLayout {
                Layout.fillWidth: true; Layout.fillHeight: true
                spacing: 0

                // Toolbar
                Rectangle {
                    Layout.fillWidth: true; height: 48
                    color: Theme.surface

                    // Bottom hairline
                    Rectangle {
                        anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
                        height: 1; color: Theme.line
                    }

                    RowLayout {
                        anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                        spacing: Theme.sp1

                        // Breadcrumb with Lucide chevrons
                        Repeater {
                            model: filesView.breadcrumb
                            delegate: RowLayout {
                                spacing: 4

                                // Chevron separator
                                Image {
                                    visible: index > 0
                                    width: 12; height: 12
                                    source: Theme.dimIcon("icons/chevron-right-dim.svg")
                                    fillMode: Image.PreserveAspectFit
                                    smooth: true; mipmap: true
                                    opacity: 0.5
                                }

                                Text {
                                    text: modelData
                                    color: index === filesView.breadcrumb.length - 1 ? Theme.ink : Theme.ink3
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsCaption + 1
                                    font.weight: index === filesView.breadcrumb.length - 1
                                        ? Font.Medium
                                        : Font.Normal
                                }
                            }
                        }

                        Item { Layout.fillWidth: true }

                        // Search button
                        Item {
                            width: 30; height: 30

                            Rectangle {
                                anchors.fill: parent; radius: Theme.rSm
                                color: searchBtnHover.containsMouse ? Theme.card : "transparent"
                                Behavior on color { ColorAnimation { duration: 100 } }
                            }

                            Image {
                                anchors.centerIn: parent
                                width: 15; height: 15
                                source: Theme.dimIcon("icons/search-dim.svg")
                                fillMode: Image.PreserveAspectFit
                                smooth: true; mipmap: true
                            }

                            MouseArea {
                                id: searchBtnHover
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                            }
                        }

                        // New folder button — Lucide folder-plus
                        Rectangle {
                            height: 28; radius: Theme.rSm
                            color: Theme.card2
                            border.color: Theme.line; border.width: 1
                            implicitWidth: newFolderRow.width + Theme.sp3

                            RowLayout {
                                id: newFolderRow
                                anchors.centerIn: parent
                                spacing: 6

                                Image {
                                    width: 14; height: 14
                                    source: Theme.dimIcon("icons/folder-plus-dim.svg")
                                    fillMode: Image.PreserveAspectFit
                                    smooth: true; mipmap: true
                                }

                                Text {
                                    text: "Nueva carpeta"
                                    color: Theme.ink2
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsCaption
                                }
                            }

                            MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor }
                        }

                        // Grid/list toggle — Lucide icons
                        Rectangle {
                            width: 60; height: 28; radius: Theme.rSm
                            color: Theme.card2; border.color: Theme.line; border.width: 1

                            RowLayout {
                                anchors.fill: parent; spacing: 0

                                // Grid mode
                                Rectangle {
                                    Layout.fillWidth: true; height: parent.height; radius: Theme.rSm
                                    color: filesView.gridMode ? Theme.alpha(Theme.accent, 0.18) : "transparent"

                                    Image {
                                        anchors.centerIn: parent
                                        width: 14; height: 14
                                        source: Theme.dimIcon("icons/grid-2x2-dim.svg")
                                        fillMode: Image.PreserveAspectFit
                                        smooth: true; mipmap: true
                                        opacity: filesView.gridMode ? 1.0 : 0.5
                                    }

                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: filesView.gridMode = true
                                    }
                                }

                                // List mode
                                Rectangle {
                                    Layout.fillWidth: true; height: parent.height; radius: Theme.rSm
                                    color: !filesView.gridMode ? Theme.alpha(Theme.accent, 0.18) : "transparent"

                                    Image {
                                        anchors.centerIn: parent
                                        width: 14; height: 14
                                        source: Theme.dimIcon("icons/list-dim.svg")
                                        fillMode: Image.PreserveAspectFit
                                        smooth: true; mipmap: true
                                        opacity: !filesView.gridMode ? 1.0 : 0.5
                                    }

                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: filesView.gridMode = false
                                    }
                                }
                            }
                        }
                    }
                }

                // File area
                Item {
                    Layout.fillWidth: true; Layout.fillHeight: true

                    // GRID VIEW
                    Flow {
                        id: fileGrid
                        anchors { fill: parent; margins: Theme.sp2 + 4 }
                        spacing: Theme.sp1 + 2
                        visible: filesView.gridMode

                        Repeater {
                            model: filesView.files
                            delegate: Item {
                                id: fileCardItem
                                width: 112; height: 108

                                // Static shadow
                                Rectangle {
                                    anchors { left: parent.left; right: parent.right; top: parent.top }
                                    anchors.topMargin: 2; anchors.leftMargin: 1; anchors.rightMargin: -1
                                    height: parent.height; radius: Theme.rLg
                                    color: "#000000"; opacity: 0.14
                                }

                                Rectangle {
                                    id: fileCard
                                    anchors.fill: parent; radius: Theme.rLg
                                    color: fileCardHover.containsMouse ? Theme.surface2 : Theme.card
                                    border.color: fileCardHover.containsMouse
                                        ? Theme.alpha(Theme.accentBright, 0.40)
                                        : Theme.line
                                    border.width: 1
                                    Behavior on color { ColorAnimation { duration: 120 } }
                                    Behavior on border.color { ColorAnimation { duration: 120 } }

                                    // Inner top hairline
                                    Rectangle {
                                        anchors { top: parent.top; left: parent.left; right: parent.right }
                                        anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                                        height: 1; radius: Theme.rLg - 1
                                        color: "#FFFFFF"; opacity: 0.04
                                    }

                                    ColumnLayout {
                                        anchors {
                                            fill: parent
                                            topMargin: Theme.sp2
                                            bottomMargin: Theme.sp1 + 2
                                            leftMargin: Theme.sp1
                                            rightMargin: Theme.sp1
                                        }
                                        spacing: Theme.sp1

                                        // Icon tile
                                        Rectangle {
                                            Layout.alignment: Qt.AlignHCenter
                                            width: 44; height: 44; radius: Theme.rMd
                                            color: modelData.kind === "folder"
                                                ? Theme.alpha(Theme.accent, 0.18)
                                                : Theme.alpha(Theme.surface2, 0.9)
                                            border.color: modelData.kind === "folder"
                                                ? Theme.alpha(Theme.accentBright, 0.16)
                                                : Theme.line
                                            border.width: 1

                                            Image {
                                                anchors.centerIn: parent
                                                width: 22; height: 22
                                                source: Theme.dimIcon(modelData.icon)
                                                fillMode: Image.PreserveAspectFit
                                                smooth: true; mipmap: true
                                                opacity: modelData.kind === "folder" ? 1.0 : 0.75
                                            }
                                        }

                                        Text {
                                            Layout.alignment: Qt.AlignHCenter
                                            Layout.fillWidth: true
                                            text: modelData.name
                                            color: Theme.ink2
                                            font.family: Theme.font
                                            font.pixelSize: Theme.tsMicro
                                            horizontalAlignment: Text.AlignHCenter
                                            wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                                            maximumLineCount: 2
                                            elide: Text.ElideRight
                                        }
                                    }

                                    MouseArea {
                                        id: fileCardHover
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        cursorShape: Qt.PointingHandCursor
                                        onDoubleClicked: {
                                            if (modelData.kind === "folder" && modelData.path)
                                                filesView.openDir(modelData.path)
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // LIST VIEW
                    ColumnLayout {
                        anchors { fill: parent; margins: Theme.sp2 }
                        spacing: 2
                        visible: !filesView.gridMode

                        // Header row
                        RowLayout {
                            Layout.fillWidth: true; spacing: 0

                            Item { width: 40 }

                            Text {
                                Layout.fillWidth: true
                                text: "Nombre"
                                color: Theme.ink4
                                font.family: Theme.font
                                font.pixelSize: Theme.tsMicro
                                font.weight: Font.Medium
                                font.letterSpacing: 0.8
                            }

                            Text {
                                Layout.preferredWidth: 160
                                text: "Tamaño        Modificado"
                                color: Theme.ink4
                                font.family: Theme.font
                                font.pixelSize: Theme.tsMicro
                                font.weight: Font.Medium
                                font.letterSpacing: 0.8
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true; height: 1; color: Theme.line
                        }

                        Repeater {
                            model: filesView.files
                            delegate: Item {
                                Layout.fillWidth: true
                                height: 38

                                Rectangle {
                                    anchors.fill: parent; radius: Theme.rSm
                                    color: listRowHover.containsMouse
                                        ? Theme.alpha(Theme.surface2, 0.7)
                                        : "transparent"
                                    Behavior on color { ColorAnimation { duration: 100 } }
                                }

                                RowLayout {
                                    anchors { fill: parent; leftMargin: Theme.sp1; rightMargin: Theme.sp1 }
                                    spacing: 8

                                    Image {
                                        width: 16; height: 16
                                        source: Theme.dimIcon(modelData.icon)
                                        fillMode: Image.PreserveAspectFit
                                        smooth: true; mipmap: true
                                        opacity: modelData.kind === "folder" ? 1.0 : 0.7
                                    }

                                    Text {
                                        Layout.fillWidth: true
                                        text: modelData.name
                                        color: Theme.ink2
                                        font.family: Theme.font
                                        font.pixelSize: Theme.tsCaption + 1
                                        elide: Text.ElideRight
                                    }

                                    Text {
                                        Layout.preferredWidth: 160
                                        text: modelData.meta
                                        color: Theme.ink4
                                        font.family: Theme.mono
                                        font.pixelSize: Theme.tsMicro
                                    }
                                }

                                MouseArea {
                                    id: listRowHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                }
                            }
                        }

                        Item { Layout.fillHeight: true }
                    }
                }

                // Status bar
                Rectangle {
                    Layout.fillWidth: true; height: 28
                    color: Theme.bg0

                    Rectangle {
                        anchors { top: parent.top; left: parent.left; right: parent.right }
                        height: 1; color: Theme.line
                    }

                    RowLayout {
                        anchors { fill: parent; leftMargin: Theme.sp2; rightMargin: Theme.sp2 }
                        spacing: Theme.sp2

                        Text {
                            text: filesView.files.length + " elementos"
                            color: Theme.ink4
                            font.family: Theme.font
                            font.pixelSize: Theme.tsMicro
                        }

                        Item { Layout.fillWidth: true }

                        Text {
                            text: "Ordenado por: Nombre"
                            color: Theme.ink4
                            font.family: Theme.font
                            font.pixelSize: Theme.tsMicro
                        }
                    }
                }
            }
        }
    }
}
