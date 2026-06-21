import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "."

// Lumen — Tareas. Agent task queue with HITL permission gates.
// Design: real Lucide line-icons, neutral dark palette, 8pt grid.
// No emoji. No MultiEffect. No RotationAnimator/loops:Infinite.
Item {
    id: tasksView
    property var shell: null

    // Datos REALES del daemon (cola de tareas del agente). Cero mock.
    ListModel { id: allTasks }

    function _waitingCount() {
        var n = 0
        for (var i = 0; i < allTasks.count; i++) if (allTasks.get(i).status === "waiting") n++
        return n
    }
    function _countBy(st) {
        var n = 0
        for (var i = 0; i < allTasks.count; i++) if (allTasks.get(i).status === st) n++
        return n
    }

    // Normaliza el estado real del work-queue → estados de la vista.
    function _mapStatus(s) {
        s = (s || "").toLowerCase()
        if (s.indexOf("progress") >= 0) return "running"
        if (s.indexOf("complet") >= 0 || s.indexOf("done") >= 0) return "done"
        if (s.indexOf("approval") >= 0 || s.indexOf("waiting") >= 0) return "waiting"
        if (s.indexOf("fail") >= 0 || s.indexOf("reject") >= 0) return "done"
        return "queued"
    }
    function _kindFromTrigger(tk) {
        tk = (tk || "").toLowerCase()
        if (tk.indexOf("chat") >= 0) return "mail"
        if (tk.indexOf("timer") >= 0 || tk.indexOf("sched") >= 0) return "bell"
        if (tk.indexOf("file") >= 0 || tk.indexOf("download") >= 0) return "download"
        return "list-checks"
    }
    function _relTime(iso) {
        if (!iso) return ""
        var t = Date.parse(iso)
        if (isNaN(t)) return ""
        var s = Math.max(0, Math.floor((Date.now() - t) / 1000))
        if (s < 60) return "Hace " + s + " s"
        if (s < 3600) return "Hace " + Math.floor(s / 60) + " min"
        if (s < 86400) return "Hace " + Math.floor(s / 3600) + " h"
        return "Hace " + Math.floor(s / 86400) + " d"
    }

    function _populate(json) {
        var arr = []
        try { arr = JSON.parse(json) } catch (e) { arr = [] }
        allTasks.clear()
        for (var i = 0; i < arr.length; i++) {
            var r = arr[i]
            var st = _mapStatus(r.status)
            allTasks.append({
                taskKind: _kindFromTrigger(r.trigger_kind),
                title: (r.label && r.label.length) ? r.label : (r.trigger_kind || "Tarea"),
                subInfo: (r.trigger_kind || "") + (r.status ? " · " + r.status : ""),
                status: st,
                timestamp: _relTime(r.enqueued_at),
                sensitiveAction: st === "waiting",
                actionLabel: (st === "done") ? "Ver" : ""
            })
        }
    }

    Connections {
        target: backend
        function onListLoaded(key, json) { if (key === "recent_tasks") tasksView._populate(json) }
    }
    Component.onCompleted: backend.loadList("recent_tasks", 50)
    // Refresco periódico de la actividad real.
    Timer { interval: 4000; running: true; repeat: true; onTriggered: backend.loadList("recent_tasks", 50) }

    property string activeFilter: "all"

    property var filteredTasks: {
        var result = []
        for (var i = 0; i < allTasks.count; i++) {
            var t = allTasks.get(i)
            if (tasksView.activeFilter === "all") result.push(i)
            else if (tasksView.activeFilter === "running" && t.status === "running") result.push(i)
            else if (tasksView.activeFilter === "done"    && t.status === "done")    result.push(i)
            else if (tasksView.activeFilter === "waiting" && t.status === "waiting") result.push(i)
        }
        return result
    }

    // Map taskKind string to Lucide icon path
    function taskIcon(kind) {
        if (kind === "files")    return "icons/folder-dim.svg"
        if (kind === "mail")     return "icons/mail-dim.svg"
        if (kind === "calendar") return "icons/calendar-dim.svg"
        if (kind === "download") return "icons/download-dim.svg"
        if (kind === "music")    return "icons/music-dim.svg"
        if (kind === "package")  return "icons/package-dim.svg"
        if (kind === "image")    return "icons/image-dim.svg"
        if (kind === "bell")     return "icons/bell-dim.svg"
        return "icons/list-checks-dim.svg"
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ── header ─────────────────────────────────────────────────────────
        Item {
            Layout.fillWidth: true; height: 60

            RowLayout {
                anchors { fill: parent; leftMargin: Theme.sp3; rightMargin: Theme.sp3 }
                spacing: Theme.sp2

                ColumnLayout {
                    spacing: 4

                    Text {
                        text: "Tareas"
                        color: Theme.ink
                        font.family: Theme.font
                        font.pixelSize: Theme.tsTitle
                        font.weight: Font.DemiBold
                    }

                    Text {
                        text: allTasks.count + " tareas" + (tasksView._waitingCount() > 0 ? " · " + tasksView._waitingCount() + " esperando tu permiso" : "")
                        color: Theme.ink3
                        font.family: Theme.font
                        font.pixelSize: Theme.tsCaption
                    }
                }

                Item { Layout.fillWidth: true }

                // Security note — Lucide shield-check icon
                RowLayout {
                    spacing: 6

                    Image {
                        width: 13; height: 13
                        source: Theme.accentIcon("icons/shield-check-accent.svg")
                        fillMode: Image.PreserveAspectFit
                        smooth: true; mipmap: true
                        opacity: 0.70
                    }

                    Text {
                        text: "Nada sale sin tu permiso"
                        color: Theme.ink3
                        font.family: Theme.font
                        font.pixelSize: Theme.tsMicro
                    }
                }
            }
        }

        // ── filter chips ────────────────────────────────────────────────────
        Item {
            Layout.fillWidth: true; height: 44

            Row {
                anchors { left: parent.left; leftMargin: Theme.sp3; verticalCenter: parent.verticalCenter }
                spacing: Theme.sp1

                Repeater {
                    model: [
                        { label: "Todas",             key: "all",     count: allTasks.count },
                        { label: "En curso",           key: "running", count: tasksView._countBy("running") },
                        { label: "Completadas",        key: "done",    count: tasksView._countBy("done") },
                        { label: "Esperando permiso", key: "waiting", count: tasksView._waitingCount() }
                    ]

                    Rectangle {
                        property bool isActive: tasksView.activeFilter === modelData.key
                        height: 30; radius: Theme.rSm
                        implicitWidth: chipRow.width + Theme.sp3
                        color: isActive ? Theme.alpha(Theme.accent, 0.20) : Theme.alpha(Theme.card2, 0.8)
                        border.color: isActive ? Theme.alpha(Theme.accentBright, 0.40) : Theme.line
                        border.width: 1
                        Behavior on color { ColorAnimation { duration: 120 } }

                        Row {
                            id: chipRow
                            anchors.centerIn: parent
                            spacing: 5

                            Text {
                                text: modelData.label
                                anchors.verticalCenter: parent.verticalCenter
                                color: isActive ? Theme.ink : Theme.ink2
                                font.family: Theme.font
                                font.pixelSize: Theme.tsCaption + 1
                                font.weight: isActive ? Font.Medium : Font.Normal
                            }

                            // Warning count pill — only on "waiting" chip
                            Rectangle {
                                visible: modelData.key === "waiting"
                                width: warnBadge.width + Theme.sp1
                                height: 16; radius: Theme.rSm - 4
                                color: Theme.alpha(Theme.warn, 0.20)
                                anchors.verticalCenter: parent.verticalCenter

                                Text {
                                    id: warnBadge
                                    anchors.centerIn: parent
                                    text: modelData.count
                                    color: Theme.warn
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsMicro
                                    font.weight: Font.DemiBold
                                }
                            }
                        }

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: tasksView.activeFilter = modelData.key
                        }
                    }
                }
            }
        }

        // Hairline divider
        Rectangle { Layout.fillWidth: true; height: 1; color: Theme.line }

        // ── task list ───────────────────────────────────────────────────────
        ListView {
            id: taskList
            Layout.fillWidth: true; Layout.fillHeight: true
            clip: true
            topMargin: Theme.sp1 + 4
            bottomMargin: Theme.sp2
            spacing: 0
            boundsBehavior: Flickable.StopAtBounds

            WheelScroll { target: taskList }

            ScrollBar.vertical: ScrollBar {
                policy: ScrollBar.AsNeeded
                contentItem: Rectangle {
                    radius: 2
                    color: Theme.alpha(Theme.ink3, 0.28)
                }
            }

            model: tasksView.filteredTasks

            delegate: Item {
                id: taskRow
                width: taskList.width
                height: taskCard.height + 6

                property int taskIndex: modelData
                property var task: allTasks.get(taskIndex)
                property bool isWaiting: task.status === "waiting"
                property bool isRunning: task.status === "running"
                property bool isDone:    task.status === "done"
                property bool isQueued:  task.status === "queued"

                // Static shadow
                Rectangle {
                    anchors {
                        left: parent.left; right: parent.right
                        leftMargin: Theme.sp3 + 1; rightMargin: Theme.sp3 - 1
                        top: parent.top; topMargin: 2
                    }
                    height: taskCard.height; radius: Theme.rLg
                    color: "#000000"; opacity: 0.14
                }

                Rectangle {
                    id: taskCard
                    anchors {
                        left: parent.left; right: parent.right
                        leftMargin: Theme.sp3; rightMargin: Theme.sp3
                    }
                    height: taskCardContent.height + Theme.sp3
                    radius: Theme.rLg
                    color: taskRow.isWaiting
                        ? (Theme.mode === "light"
                           ? Theme.alpha(Theme.warn, 0.06)
                           : Theme.alpha("#160F08", 0.98))
                        : Theme.card
                    border.color: taskRow.isWaiting
                        ? Theme.alpha(Theme.warn, 0.44)
                        : taskRow.isRunning
                            ? Theme.alpha(Theme.accent, 0.36)
                            : Theme.line
                    border.width: 1

                    // Inner top hairline
                    Rectangle {
                        anchors { top: parent.top; left: parent.left; right: parent.right }
                        anchors.topMargin: 1; anchors.leftMargin: 1; anchors.rightMargin: 1
                        height: 1; radius: Theme.rLg - 1
                        color: "#FFFFFF"
                        opacity: taskRow.isWaiting ? 0.03 : 0.04
                    }

                    // Running left accent strip — static, no pulsing
                    Rectangle {
                        visible: taskRow.isRunning
                        width: 3; height: parent.height - Theme.sp3; radius: 2
                        anchors { left: parent.left; leftMargin: 1; verticalCenter: parent.verticalCenter }
                        color: Theme.accentBright; opacity: 0.70
                    }

                    ColumnLayout {
                        id: taskCardContent
                        anchors {
                            left: parent.left; right: parent.right; top: parent.top
                            margins: Theme.sp2; topMargin: Theme.sp1 + 4
                        }
                        spacing: Theme.sp1

                        // Top row: icon + title + badge + timestamp
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.sp1 + 4

                            // Task icon tile — Lucide icon, no emoji
                            Rectangle {
                                width: 38; height: 38; radius: Theme.rMd
                                color: taskRow.isWaiting ? Theme.alpha(Theme.warn, 0.12)
                                     : taskRow.isRunning ? Theme.alpha(Theme.accent, 0.16)
                                     : taskRow.isDone    ? Theme.alpha(Theme.ok, 0.10)
                                     : Theme.alpha(Theme.surface2, 0.7)
                                border.color: taskRow.isWaiting ? Theme.alpha(Theme.warn, 0.20)
                                            : taskRow.isRunning ? Theme.alpha(Theme.accentBright, 0.18)
                                            : taskRow.isDone    ? Theme.alpha(Theme.ok, 0.16)
                                            : Theme.line
                                border.width: 1

                                Image {
                                    anchors.centerIn: parent
                                    width: 18; height: 18
                                    source: Theme.dimIcon(tasksView.taskIcon(taskRow.task.taskKind))
                                    fillMode: Image.PreserveAspectFit
                                    smooth: true; mipmap: true
                                    opacity: taskRow.isWaiting ? 0.90
                                           : taskRow.isDone    ? 0.65
                                           : 0.85
                                }
                            }

                            // Title + subInfo
                            Column {
                                Layout.fillWidth: true
                                spacing: 3

                                Text {
                                    text: taskRow.task.title
                                    color: Theme.ink
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsBody
                                    font.weight: taskRow.isWaiting ? Font.DemiBold : Font.Medium
                                    width: parent.width
                                    elide: Text.ElideRight
                                }

                                Text {
                                    text: taskRow.task.subInfo
                                    color: Theme.ink3
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsCaption
                                    width: parent.width
                                    elide: Text.ElideRight
                                }
                            }

                            // Status badge + timestamp column
                            Column {
                                spacing: 4

                                // Status pill — Lucide icon + label, no emoji
                                Rectangle {
                                    height: 20; radius: Theme.rSm - 2
                                    implicitWidth: statusRow.width + Theme.sp1 + 4
                                    anchors.right: parent.right
                                    color: taskRow.isWaiting ? Theme.alpha(Theme.warn, 0.16)
                                         : taskRow.isRunning ? Theme.alpha(Theme.accent, 0.18)
                                         : taskRow.isDone    ? Theme.alpha(Theme.ok, 0.13)
                                         : Theme.alpha(Theme.card2, 0.8)
                                    border.color: taskRow.isWaiting ? Theme.alpha(Theme.warn, 0.48)
                                                : taskRow.isRunning ? Theme.alpha(Theme.accentBright, 0.38)
                                                : taskRow.isDone    ? Theme.alpha(Theme.ok, 0.36)
                                                : Theme.line
                                    border.width: 1

                                    Row {
                                        id: statusRow
                                        anchors.centerIn: parent
                                        spacing: 4

                                        // Status icon — Lucide line icon
                                        Image {
                                            width: 11; height: 11
                                            anchors.verticalCenter: parent.verticalCenter
                                            source: taskRow.isWaiting ? "icons/alert-circle-warn.svg"
                                                  : taskRow.isRunning ? "icons/play-accent.svg"
                                                  : taskRow.isDone    ? "icons/circle-check-ok.svg"
                                                  : "icons/clock-dim.svg"
                                            fillMode: Image.PreserveAspectFit
                                            smooth: true; mipmap: true
                                        }

                                        Text {
                                            anchors.verticalCenter: parent.verticalCenter
                                            text: taskRow.isWaiting ? "Tu permiso"
                                                : taskRow.isRunning ? "En curso"
                                                : taskRow.isDone    ? "Completada"
                                                : "Pendiente"
                                            color: taskRow.isWaiting ? Theme.warn
                                                 : taskRow.isRunning ? Theme.accentBright
                                                 : taskRow.isDone    ? Theme.ok
                                                 : Theme.ink4
                                            font.family: Theme.font
                                            font.pixelSize: Theme.tsMicro
                                            font.weight: taskRow.isWaiting ? Font.DemiBold : Font.Normal
                                        }
                                    }
                                }

                                Text {
                                    text: taskRow.task.timestamp
                                    color: Theme.ink4
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsMicro
                                    anchors.right: parent.right
                                }
                            }
                        }

                        // Static progress bar for running tasks
                        Rectangle {
                            visible: taskRow.isRunning
                            Layout.fillWidth: true; height: 3; radius: 2
                            color: Theme.alpha(Theme.accent, 0.18)

                            Rectangle {
                                height: parent.height; radius: parent.radius
                                width: parent.width * 0.62
                                color: Theme.accentBright; opacity: 0.85
                            }
                        }

                        // HITL permission section for waiting tasks
                        Rectangle {
                            visible: taskRow.isWaiting
                            Layout.fillWidth: true; height: 1
                            color: Theme.alpha(Theme.warn, 0.18)
                        }

                        RowLayout {
                            visible: taskRow.isWaiting
                            Layout.fillWidth: true
                            spacing: Theme.sp1

                            RowLayout {
                                spacing: 6
                                Layout.fillWidth: true

                                Image {
                                    width: 13; height: 13
                                    source: Theme.accentIcon("icons/shield-check-accent.svg")
                                    fillMode: Image.PreserveAspectFit
                                    smooth: true; mipmap: true
                                    opacity: 0.80
                                }

                                Text {
                                    text: "Lumen necesita tu aprobación para continuar."
                                    color: Theme.alpha(Theme.warn, 0.80)
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsCaption
                                    Layout.fillWidth: true
                                    wrapMode: Text.WordWrap
                                }
                            }

                            // Deny button
                            Rectangle {
                                height: 30; radius: Theme.rSm
                                color: "transparent"
                                border.color: Theme.line; border.width: 1
                                implicitWidth: denyTxt.width + Theme.sp2

                                Text {
                                    id: denyTxt
                                    anchors.centerIn: parent
                                    text: "Denegar"
                                    color: Theme.ink3
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsCaption
                                }

                                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor }
                            }

                            // Approve button
                            Rectangle {
                                height: 30; radius: Theme.rSm
                                color: Theme.alpha(Theme.warn, 0.18)
                                border.color: Theme.alpha(Theme.warn, 0.48); border.width: 1
                                implicitWidth: approveTxt.width + Theme.sp3

                                Text {
                                    id: approveTxt
                                    anchors.centerIn: parent
                                    text: taskRow.task.actionLabel.length > 0
                                        ? taskRow.task.actionLabel
                                        : "Aprobar"
                                    color: Theme.warn
                                    font.family: Theme.font
                                    font.pixelSize: Theme.tsCaption
                                    font.weight: Font.DemiBold
                                }

                                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor }
                            }
                        }

                        // "Ver resultado" link for done tasks
                        Item {
                            visible: taskRow.isDone && taskRow.task.actionLabel.length > 0
                            Layout.fillWidth: true; height: 18

                            Item {
                                anchors { right: parent.right; top: parent.top; bottom: parent.bottom }
                                width: verRow.width

                                Row {
                                    id: verRow
                                    anchors.verticalCenter: parent.verticalCenter
                                    spacing: 4

                                    Text {
                                        text: "Ver resultado"
                                        color: Theme.accentBright
                                        font.family: Theme.font
                                        font.pixelSize: Theme.tsCaption
                                        font.weight: Font.Medium
                                        anchors.verticalCenter: parent.verticalCenter
                                    }

                                    Image {
                                        width: 12; height: 12
                                        source: Theme.dimIcon("icons/arrow-right-dim.svg")
                                        fillMode: Image.PreserveAspectFit
                                        smooth: true; mipmap: true
                                        opacity: 0.7
                                        anchors.verticalCenter: parent.verticalCenter
                                    }
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: { if (tasksView.shell) tasksView.shell.go(1) }
                                }
                            }
                        }
                    }
                }
            }

            // Empty state — Lucide list-checks icon, no emoji
            Item {
                visible: tasksView.filteredTasks.length === 0
                width: taskList.width; height: taskList.height

                Column {
                    anchors.centerIn: parent
                    spacing: Theme.sp1

                    // Icon ring
                    Item {
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: 64; height: 64

                        Rectangle {
                            anchors.fill: parent; radius: 32
                            color: Theme.alpha(Theme.card2, 0.7)
                            border.color: Theme.line; border.width: 1
                        }

                        Image {
                            anchors.centerIn: parent
                            width: 28; height: 28
                            source: Theme.dimIcon("icons/list-checks-dim.svg")
                            fillMode: Image.PreserveAspectFit
                            smooth: true; mipmap: true
                        }
                    }

                    Item { height: 4 }

                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: "Sin tareas en este filtro"
                        color: Theme.ink3
                        font.family: Theme.font
                        font.pixelSize: Theme.tsBody
                    }

                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: "Pídele algo a Lumen desde Inicio o Chat"
                        color: Theme.ink4
                        font.family: Theme.font
                        font.pixelSize: Theme.tsCaption
                    }
                }
            }

            footer: Item { height: Theme.sp1 }
        }
    }
}
