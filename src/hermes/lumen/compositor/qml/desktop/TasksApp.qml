import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// ── TasksApp ──────────────────────────────────────────────────────────────
// Módulo de tareas programadas por agente. Dos vistas: Tablero (todas las
// tareas configuradas, global) y Calendario (por agente, rejilla semanal).
// Cableado REAL al daemon vía bridge hermes.call:
//   list_agents / list_configured_tasks / list_recent_tasks /
//   create_scheduled_task / delete_scheduled_task / set_scheduled_task_enabled
Rectangle {
    id: app
    anchors.fill: parent
    color: Tokens.bgSurface
    radius: Tokens.radiusLg
    readonly property real sf: root.sf
    readonly property bool _compact: app.width < Tokens.bpCompact * root.sf

    // ── State ─────────────────────────────────────────────────────────────
    property var agents: []
    property var configuredTasks: []
    property bool loadingAgents: true
    property bool loadingTasks: true

    // Pestaña activa: "board" | "calendar"
    property string activeTab: "board"

    // Agente seleccionado en Calendario
    property string calAgentId: ""

    // Formulario nueva tarea
    property bool showForm: false
    property int formPreselDay: -1   // 0=Lun … 6=Dom; -1 = sin preselección

    // Error/feedback global
    property string globalNote: ""
    property bool globalNoteOk: false

    // Counter para reqIds únicos
    property int reqCounter: 0

    function nextReqId(prefix) {
        reqCounter++;
        return "task-" + prefix + "-" + reqCounter;
    }

    // ── Helpers de fecha ─────────────────────────────────────────────────
    function formatDate(isoStr) {
        if (!isoStr || isoStr === "null") return "—";
        var d = new Date(isoStr);
        if (isNaN(d.getTime())) return "—";
        var now = new Date();
        var dayNames = ["Dom", "Lun", "Mar", "Mié", "Jue", "Vie", "Sáb"];
        var monNames = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
        var hh = ("0" + d.getHours()).slice(-2);
        var mm = ("0" + d.getMinutes()).slice(-2);
        if (d.toDateString() === now.toDateString()) return "Hoy " + hh + ":" + mm;
        return dayNames[d.getDay()] + " " + d.getDate() + " " + monNames[d.getMonth()] + ", " + hh + ":" + mm;
    }

    // ── Resolución de agente ─────────────────────────────────────────────
    function agentName(agentId) {
        if (!agentId || agentId === "null") return "Cualquiera";
        for (var i = 0; i < agents.length; i++) {
            if (agents[i].agent_id === agentId) return agents[i].name || agentId;
        }
        return agentId;
    }

    // ── Parser de cron (día-de-semana) ───────────────────────────────────
    // Devuelve array de índices de día de la semana [0..6] donde 0=Lun, 6=Dom.
    // Cron DOW: 0/7=domingo, 1=lunes … 6=sábado.
    function cronDaysOfWeek(cron) {
        if (!cron || cron.trim().length === 0) return [];
        var fields = cron.trim().split(/\s+/);
        // Cron estándar Unix de 5 campos: min hr dom mon dow (lo que el daemon
        // valida y croniter espera). Tolera 6 campos legacy (con segundos).
        if (fields.length < 5) return [];
        var dow = fields.length >= 6 ? fields[5] : fields[4];  // día de semana
        if (dow === "*") return [0, 1, 2, 3, 4, 5, 6];
        var result = [];
        var parts = dow.split(",");
        for (var p = 0; p < parts.length; p++) {
            var part = parts[p].trim();
            if (part.indexOf("-") >= 0) {
                var range = part.split("-");
                var from = parseInt(range[0], 10);
                var to = parseInt(range[1], 10);
                for (var d = from; d <= to; d++) {
                    var idx = cronDowToAppIdx(d);
                    if (idx >= 0 && result.indexOf(idx) < 0) result.push(idx);
                }
            } else {
                var num = parseInt(part, 10);
                if (!isNaN(num)) {
                    var ai = cronDowToAppIdx(num);
                    if (ai >= 0 && result.indexOf(ai) < 0) result.push(ai);
                }
            }
        }
        return result;
    }

    // Cron DOW 0/7=domingo → app 6=Dom; 1=Lun → app 0; … 6=Sáb → app 5
    function cronDowToAppIdx(cronDow) {
        if (cronDow === 0 || cronDow === 7) return 6;
        if (cronDow >= 1 && cronDow <= 6) return cronDow - 1;
        return -1;
    }

    // Extrae hora:minuto del cron. 5 campos: min hr dom mon dow (campo 0=min,
    // 1=hr). Tolera 6 campos legacy (sec min hr → 1=min, 2=hr).
    function cronTime(cron) {
        if (!cron || cron.trim().length === 0) return "?:??";
        var fields = cron.trim().split(/\s+/);
        if (fields.length < 5) return "?:??";
        var sixField = fields.length >= 6;
        var mn = sixField ? fields[1] : fields[0];
        var hr = sixField ? fields[2] : fields[1];
        if (mn === "*" || hr === "*") return "?:??";
        return ("0" + hr).slice(-2) + ":" + ("0" + mn).slice(-2);
    }

    // ── Carga datos ──────────────────────────────────────────────────────
    function loadAll() {
        loadingAgents = true;
        loadingTasks = true;
        hermes.call("task-agents", "list_agents", "{}");
        hermes.call("task-list", "list_configured_tasks", JSON.stringify({ limit: 200 }));
    }

    // ── Connections ──────────────────────────────────────────────────────
    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "task-agents") {
                app.loadingAgents = false;
                try { app.agents = ok ? JSON.parse(jsonStr || "[]") : []; }
                catch (e) { app.agents = []; }
                // Preseleccionar primer agente en calendario si no hay ninguno
                if (app.calAgentId === "" && app.agents.length > 0) {
                    app.calAgentId = app.agents[0].agent_id || "";
                }
                return;
            }
            if (reqId === "task-list") {
                app.loadingTasks = false;
                try { app.configuredTasks = ok ? JSON.parse(jsonStr || "[]") : []; }
                catch (e) { app.configuredTasks = []; }
                return;
            }
            if (reqId.indexOf("task-toggle-") === 0) {
                if (!ok) {
                    try { app.globalNote = JSON.parse(jsonStr).error || jsonStr; app.globalNoteOk = false; }
                    catch (e) { app.globalNote = jsonStr || "Error al cambiar estado"; app.globalNoteOk = false; }
                }
                hermes.call("task-list", "list_configured_tasks", JSON.stringify({ limit: 200 }));
                return;
            }
            if (reqId.indexOf("task-delete-") === 0) {
                if (!ok) {
                    try { app.globalNote = JSON.parse(jsonStr).error || jsonStr; app.globalNoteOk = false; }
                    catch (e) { app.globalNote = jsonStr || "Error al borrar tarea"; app.globalNoteOk = false; }
                }
                hermes.call("task-list", "list_configured_tasks", JSON.stringify({ limit: 200 }));
                return;
            }
            if (reqId === "task-create") {
                try {
                    var r = JSON.parse(jsonStr || "{}");
                    if (ok && r.ok) {
                        app.globalNote = "Tarea creada";
                        app.globalNoteOk = true;
                        app.showForm = false;
                        hermes.call("task-list", "list_configured_tasks", JSON.stringify({ limit: 200 }));
                        root.showToast("Tarea programada", "success");
                    } else {
                        app.globalNote = r.error || jsonStr || "Error al crear tarea";
                        app.globalNoteOk = false;
                    }
                } catch (e) {
                    app.globalNote = ok ? "Tarea creada" : (jsonStr || "Error");
                    app.globalNoteOk = ok;
                    if (ok) {
                        app.showForm = false;
                        hermes.call("task-list", "list_configured_tasks", JSON.stringify({ limit: 200 }));
                    }
                }
                return;
            }
        }
    }

    Component.onCompleted: loadAll()

    // ── Layout principal ─────────────────────────────────────────────────
    Column {
        anchors.fill: parent
        anchors.margins: Math.round(Tokens.spXl * sf)
        spacing: Math.round(Tokens.spLg * sf)

        // ── Cabecera ──────────────────────────────────────────────────
        Row {
            width: parent.width
            Column {
                width: parent.width - newTaskBtn.width - Math.round(Tokens.spSm * sf)
                Text {
                    text: "Tareas"
                    color: Tokens.textPrimary
                    font.family: Tokens.fontDisplay
                    font.pixelSize: Math.round(20 * sf)
                    font.weight: Font.Medium
                }
                Text {
                    text: "Programa tareas para tus agentes: únicas o recurrentes. Tablero global y vista semanal por agente."
                    color: Tokens.textMuted
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(12 * sf)
                    width: parent.width
                    wrapMode: Text.WordWrap
                }
            }

            LumenButton {
                id: newTaskBtn
                sf: app.sf
                label: app.showForm ? "Cancelar" : "+ Nueva tarea"
                variant: app.showForm ? "secondary" : "primary"
                implicitWidth: Math.round(130 * sf)
                implicitHeight: Math.round(36 * sf)
                anchors.verticalCenter: parent.verticalCenter
                onClicked: {
                    app.showForm = !app.showForm;
                    app.globalNote = "";
                    app.formPreselDay = -1;
                }
            }
        }

        // ── Nota global ───────────────────────────────────────────────
        Text {
            visible: app.globalNote.length > 0
            text: app.globalNote
            color: app.globalNoteOk ? Tokens.successBase : Tokens.dangerBase
            font.family: Tokens.fontBody
            font.pixelSize: Math.round(12 * sf)
            width: parent.width
            wrapMode: Text.WordWrap
        }

        // ── Formulario nueva tarea ────────────────────────────────────
        TaskForm {
            id: taskForm
            visible: app.showForm
            width: parent.width
            agentsList: app.agents
            preselDay: app.formPreselDay
            sf: app.sf
            onCreateTask: function(draft) {
                hermes.call("task-create", "create_scheduled_task",
                            JSON.stringify({ draft_json: draft }));
            }
        }

        // ── Pestañas ──────────────────────────────────────────────────
        Row {
            spacing: Math.round(Tokens.spSm * sf)
            Repeater {
                model: [{ id: "board", label: "Tablero" }, { id: "calendar", label: "Calendario" }]
                Rectangle {
                    height: Math.round(30 * sf)
                    width: tabLbl.width + Math.round(Tokens.spXl * sf)
                    radius: Math.round(Tokens.radiusSm * sf)
                    color: app.activeTab === modelData.id ? Tokens.accentSubtle : Tokens.bgElevated
                    border.width: 1
                    border.color: app.activeTab === modelData.id
                                  ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.40)
                                  : Tokens.borderDefault

                    Behavior on color {
                        enabled: !Tokens.reduceMotion
                        ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                    }

                    Text {
                        id: tabLbl
                        anchors.centerIn: parent
                        text: modelData.label
                        color: app.activeTab === modelData.id ? Tokens.accentBase : Tokens.textSecondary
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(11 * sf)
                        font.weight: app.activeTab === modelData.id ? Font.DemiBold : Font.Normal
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: app.activeTab = modelData.id
                    }
                }
            }
        }

        // ── Tablero global ────────────────────────────────────────────
        Item {
            width: parent.width
            height: app.height - y - Math.round(Tokens.spXl * sf)
            visible: app.activeTab === "board"

            Text {
                visible: app.loadingTasks
                text: "Cargando…"
                color: Tokens.textMuted
                font.family: Tokens.fontBody
                font.pixelSize: Math.round(13 * sf)
            }
            Text {
                visible: !app.loadingTasks && app.configuredTasks.length === 0
                text: "No hay tareas programadas. Crea la primera con \"+ Nueva tarea\"."
                color: Tokens.textMuted
                font.family: Tokens.fontBody
                font.pixelSize: Math.round(13 * sf)
                width: parent.width
                wrapMode: Text.WordWrap
            }

            ListView {
                id: tasksList
                anchors.fill: parent
                spacing: Math.round(Tokens.spSm * sf)
                clip: true
                model: app.configuredTasks
                visible: !app.loadingTasks && app.configuredTasks.length > 0
                ScrollBar.vertical: LumenScrollBar { sf: app.sf; policy: ScrollBar.AsNeeded }

                WheelHandler {
                    acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                    onWheel: (event) => {
                        var f = tasksList;
                        f.contentY = Math.max(0, Math.min(Math.max(0, f.contentHeight - f.height), f.contentY - event.angleDelta.y));
                    }
                }

                delegate: TaskBoardCard {
                    width: ListView.view.width
                    // BUG QML: modelData de un array de objetos JS pasado a una
                    // `required property var` de un sub-component da undefined al
                    // leer sus propiedades (title/agente/next salían vacíos en el
                    // tablero). Indexar el array directamente entrega el objeto real.
                    taskData: app.configuredTasks[index]
                    sf: app.sf
                    agentDisplayName: app.agentName(app.configuredTasks[index].target_agent_id)
                    onToggleEnabled: function(triggerId, enabled) {
                        hermes.call(app.nextReqId("toggle") + "-" + triggerId,
                                    "set_scheduled_task_enabled",
                                    JSON.stringify({ trigger_id: triggerId, enabled: enabled }));
                    }
                    onDeleteTask: function(triggerId) {
                        hermes.call(app.nextReqId("delete") + "-" + triggerId,
                                    "delete_scheduled_task",
                                    JSON.stringify({ trigger_id: triggerId }));
                    }
                }
            }
        }

        // ── Calendario semanal ────────────────────────────────────────
        Item {
            width: parent.width
            height: app.height - y - Math.round(Tokens.spXl * sf)
            visible: app.activeTab === "calendar"

            Column {
                anchors.fill: parent
                spacing: Math.round(Tokens.spMd * sf)

                // Selector de agente (chips)
                Flow {
                    width: parent.width
                    spacing: Math.round(Tokens.spSm * sf)

                    Text {
                        text: "Cargando agentes…"
                        visible: app.loadingAgents
                        color: Tokens.textMuted
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(12 * sf)
                    }

                    Repeater {
                        model: app.agents
                        Rectangle {
                            height: Math.round(28 * sf)
                            width: chipLbl.width + Math.round(Tokens.spLg * sf)
                            radius: Math.round(Tokens.radiusSm * sf)
                            color: app.calAgentId === modelData.agent_id
                                   ? Tokens.accentSubtle
                                   : Tokens.bgElevated
                            border.width: 1
                            border.color: app.calAgentId === modelData.agent_id
                                          ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.40)
                                          : Tokens.borderDefault

                            Behavior on color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                            }

                            Text {
                                id: chipLbl
                                anchors.centerIn: parent
                                text: modelData.name || modelData.agent_id
                                color: app.calAgentId === modelData.agent_id
                                       ? Tokens.accentBase : Tokens.textSecondary
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(11 * sf)
                                font.weight: app.calAgentId === modelData.agent_id
                                             ? Font.DemiBold : Font.Normal
                            }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: app.calAgentId = modelData.agent_id
                            }
                        }
                    }
                }

                Text {
                    visible: !app.loadingAgents && app.agents.length === 0
                    text: "No hay agentes configurados."
                    color: Tokens.textMuted
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(13 * sf)
                }

                // Rejilla semanal Lun-Dom
                Item {
                    id: calendarGrid
                    width: parent.width
                    height: parent.height - y - Math.round(Tokens.spXs * sf)
                    clip: true
                    visible: app.calAgentId !== "" && !app.loadingAgents

                    // Filtrar tareas del agente seleccionado
                    property var agentTasks: {
                        var out = [];
                        for (var i = 0; i < app.configuredTasks.length; i++) {
                            var t = app.configuredTasks[i];
                            if (t.target_agent_id === app.calAgentId) out.push(t);
                        }
                        return out;
                    }

                    readonly property var dayLabels: ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

                    // Calendar grid — scrollable on narrow so all 7 day columns remain legible
                    Flickable {
                        anchors.fill: parent
                        contentWidth: calRow.width
                        contentHeight: height
                        clip: true
                        boundsBehavior: Flickable.StopAtBounds
                        flickableDirection: Flickable.HorizontalFlick
                        ScrollBar.horizontal: LumenScrollBar { sf: app.sf; policy: ScrollBar.AsNeeded }

                        WheelHandler {
                            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                            onWheel: (event) => {
                                parent.contentX = Math.max(0, Math.min(
                                    Math.max(0, parent.contentWidth - parent.width),
                                    parent.contentX - event.angleDelta.x
                                ));
                            }
                        }

                    Row {
                        id: calRow
                        height: parent.height
                        spacing: Math.round(Tokens.spXs * sf)
                        // Each column min 120*sf; on wide screens fill evenly
                        property real colMin: Math.round(120 * sf)
                        property int  colCount: 7
                        property real totalMin: colMin * colCount + Math.round(Tokens.spXs * sf) * (colCount - 1)
                        width: Math.max(totalMin, parent.parent.width)

                        Repeater {
                            model: 7

                            Rectangle {
                                id: dayCol
                                property int dayIdx: index
                                width: (calRow.width - Math.round(Tokens.spXs * sf) * 6) / 7
                                height: parent.height
                                radius: Math.round(Tokens.radiusSm * sf)
                                color: Tokens.bgCard
                                border.width: 1
                                border.color: Tokens.borderSubtle

                                // Tareas que caen en este día
                                property var dayTasks: {
                                    var dt = calendarGrid.agentTasks;
                                    var out = [];
                                    for (var i = 0; i < dt.length; i++) {
                                        var days = app.cronDaysOfWeek(dt[i].recurrence);
                                        if (days.indexOf(dayIdx) >= 0) out.push(dt[i]);
                                    }
                                    return out;
                                }

                                Column {
                                    anchors.fill: parent
                                    anchors.margins: Math.round(Tokens.spXs * sf)
                                    spacing: Math.round(Tokens.spXs * sf)

                                    // Cabecera del día
                                    Rectangle {
                                        width: parent.width
                                        height: Math.round(24 * sf)
                                        radius: Math.round(Tokens.radiusSm * sf)
                                        color: Tokens.bgElevated
                                        Text {
                                            anchors.centerIn: parent
                                            text: calendarGrid.dayLabels[dayCol.dayIdx]
                                            color: Tokens.textSecondary
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(10 * sf)
                                            font.weight: Font.DemiBold
                                        }
                                    }

                                    // Tareas del día (scrollable)
                                    ListView {
                                        id: calDayList
                                        width: parent.width
                                        height: parent.height - Math.round(24 * sf) - Math.round(22 * sf) - Math.round(Tokens.spXs * sf) * 2
                                        clip: true
                                        model: dayCol.dayTasks
                                        spacing: Math.round(Tokens.spXs * sf)
                                        ScrollBar.vertical: LumenScrollBar { sf: app.sf; policy: ScrollBar.AsNeeded }

                                        delegate: Rectangle {
                                            width: calDayList.width
                                            height: calTaskCol.implicitHeight + Math.round(Tokens.spSm * sf)
                                            radius: Math.round(Tokens.radiusSm * sf)
                                            color: modelData.enabled
                                                   ? Tokens.accentGhost
                                                   : Tokens.bgElevated
                                            border.width: 1
                                            border.color: modelData.enabled
                                                          ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.28)
                                                          : Tokens.borderSubtle
                                            Column {
                                                id: calTaskCol
                                                anchors.left: parent.left
                                                anchors.right: parent.right
                                                anchors.top: parent.top
                                                anchors.topMargin: Math.round(Tokens.spXs * sf)
                                                anchors.margins: Math.round(Tokens.spXs * sf)
                                                spacing: Math.round(2 * sf)
                                                Text {
                                                    text: app.cronTime(modelData.recurrence)
                                                    color: Tokens.accentBase
                                                    font.family: Tokens.fontBody
                                                    font.pixelSize: Math.round(9 * sf)
                                                    font.weight: Font.DemiBold
                                                    width: parent.width
                                                    elide: Text.ElideRight
                                                }
                                                Text {
                                                    text: modelData.title || modelData.label || "—"
                                                    color: modelData.enabled ? Tokens.textPrimary : Tokens.textMuted
                                                    font.family: Tokens.fontBody
                                                    font.pixelSize: Math.round(9 * sf)
                                                    width: parent.width
                                                    elide: Text.ElideRight
                                                    wrapMode: Text.NoWrap
                                                }
                                            }
                                        }
                                    }

                                    // Botón "+ Nueva tarea" por día
                                    Rectangle {
                                        width: parent.width
                                        height: Math.round(22 * sf)
                                        radius: Math.round(Tokens.radiusSm * sf)
                                        color: "transparent"
                                        border.width: 1
                                        border.color: addDayMa.containsMouse
                                                      ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.40)
                                                      : Tokens.borderSubtle

                                        Behavior on border.color {
                                            enabled: !Tokens.reduceMotion
                                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                        }

                                        Text {
                                            anchors.centerIn: parent
                                            text: "+"
                                            color: addDayMa.containsMouse ? Tokens.accentBase : Tokens.textMuted
                                            font.family: Tokens.fontBody
                                            font.pixelSize: Math.round(11 * sf)
                                        }
                                        MouseArea {
                                            id: addDayMa
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                app.formPreselDay = dayCol.dayIdx;
                                                app.showForm = true;
                                                app.activeTab = "board";
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    } // Flickable calendar
                }
            }
        }
    }

    // ── Subcomponente: tarjeta del tablero ────────────────────────────────
    component TaskBoardCard: Rectangle {
        id: card
        height: cardCol.implicitHeight + Math.round(Tokens.spXl * sf)
        radius: Math.round(Tokens.radiusMd * sf)
        color: Tokens.bgCard
        border.width: 1
        border.color: Tokens.borderSubtle

        required property var taskData
        required property real sf
        required property string agentDisplayName
        signal toggleEnabled(string triggerId, bool enabled)
        signal deleteTask(string triggerId)

        property bool confirmDelete: false

        // Subtle hover lift
        readonly property bool _hovered: cardMa.containsMouse

        Behavior on color {
            enabled: !Tokens.reduceMotion
            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
        }

        MouseArea {
            id: cardMa
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.NoButton
        }

        Rectangle {
            anchors.fill: parent
            radius: Math.round(Tokens.radiusMd * sf)
            color: card._hovered ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.03) : "transparent"
            Behavior on color {
                enabled: !Tokens.reduceMotion
                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
            }
        }

        Column {
            id: cardCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Math.round(Tokens.spLg * sf)
            anchors.rightMargin: Math.round(Tokens.spLg * sf)
            spacing: Math.round(Tokens.spSm * sf)

            // Fila 1: título + badges + acciones
            RowLayout {
                width: parent.width
                spacing: Math.round(Tokens.spSm * sf)

                // Pulse ring para estado running (gated en reduceMotion)
                Item {
                    Layout.alignment: Qt.AlignVCenter
                    width: Math.round(10 * sf)
                    height: Math.round(10 * sf)
                    visible: card.taskData.last_status === "in_progress"

                    Rectangle {
                        id: pulseRing
                        anchors.centerIn: parent
                        width: parent.width
                        height: parent.height
                        radius: width / 2
                        color: "transparent"
                        border.width: Math.round(1.5 * sf)
                        border.color: Tokens.warnBase
                        opacity: 0.0

                        SequentialAnimation on opacity {
                            running: card.taskData.last_status === "in_progress" && !Tokens.reduceMotion
                            loops: Animation.Infinite
                            NumberAnimation { to: 0.8; duration: Tokens.durSlow; easing.type: Easing.OutCubic }
                            NumberAnimation { to: 0.0; duration: Tokens.durSlow; easing.type: Easing.InCubic }
                        }
                        SequentialAnimation on scale {
                            running: card.taskData.last_status === "in_progress" && !Tokens.reduceMotion
                            loops: Animation.Infinite
                            NumberAnimation { to: 1.8; duration: Tokens.durSlow; easing.type: Easing.OutCubic }
                            NumberAnimation { to: 1.0; duration: Tokens.durSlow; easing.type: Easing.InCubic }
                        }
                    }

                    Rectangle {
                        anchors.centerIn: parent
                        width: Math.round(6 * sf)
                        height: Math.round(6 * sf)
                        radius: width / 2
                        color: Tokens.warnBase
                    }
                }

                // Título
                Text {
                    text: card.taskData.title || card.taskData.label || "—"
                    color: Tokens.textPrimary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(13 * sf)
                    font.weight: Font.DemiBold
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                }

                // Chip tipo recurrente / una vez
                LumenChip {
                    sf: card.sf
                    text: card.taskData.one_shot ? "Una vez" : "Recurrente"
                    tone: card.taskData.one_shot ? "neutral" : "info"
                    Layout.alignment: Qt.AlignVCenter
                }

                // Chip estado última ejecución
                LumenChip {
                    sf: card.sf
                    text: {
                        var s = card.taskData.last_status || "";
                        if (s === "completed") return "completada";
                        if (s === "failed") return "error";
                        if (s === "in_progress") return "activa";
                        return "sin ejecución";
                    }
                    tone: {
                        var s = card.taskData.last_status || "";
                        if (s === "completed") return "success";
                        if (s === "failed") return "danger";
                        if (s === "in_progress") return "warn";
                        return "neutral";
                    }
                    Layout.alignment: Qt.AlignVCenter
                }

                // Toggle habilitado (LumenSwitch)
                LumenSwitch {
                    sf: card.sf
                    checked: card.taskData.enabled
                    Layout.alignment: Qt.AlignVCenter
                    onToggled: function(v) {
                        card.toggleEnabled(card.taskData.trigger_id, v)
                    }
                }

                // Botón borrar con confirmación inline
                Rectangle {
                    id: deleteBtn
                    Layout.alignment: Qt.AlignVCenter
                    width: card.confirmDelete
                           ? Math.round(70 * sf) : Math.round(28 * sf)
                    height: Math.round(28 * sf)
                    radius: Math.round(Tokens.radiusSm * sf)
                    color: card.confirmDelete ? Tokens.dangerSubtle : "transparent"
                    border.width: 1
                    border.color: card.confirmDelete ? Tokens.dangerBase : Tokens.borderDefault

                    Behavior on width {
                        enabled: !Tokens.reduceMotion
                        NumberAnimation { duration: Tokens.durFast; easing.type: Easing.OutCubic }
                    }
                    Behavior on color {
                        enabled: !Tokens.reduceMotion
                        ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                    }

                    Text {
                        anchors.centerIn: parent
                        text: card.confirmDelete ? "Sí, borrar" : "🗑"
                        color: card.confirmDelete ? Tokens.dangerBase : Tokens.textMuted
                        font.family: Tokens.fontBody
                        font.pixelSize: card.confirmDelete
                                        ? Math.round(10 * sf) : Math.round(11 * sf)
                        elide: Text.ElideRight
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            if (!card.confirmDelete) {
                                card.confirmDelete = true;
                            } else {
                                card.confirmDelete = false;
                                card.deleteTask(card.taskData.trigger_id);
                            }
                        }
                    }
                }
            }

            // Fila 2: agente + recurrencia + próxima ejecución
            Row {
                width: parent.width
                spacing: Math.round(Tokens.spLg * sf)
                Text {
                    text: "Agente: " + card.agentDisplayName
                    color: Tokens.textMuted
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                }
                Text {
                    text: card.taskData.recurrence_human || card.taskData.recurrence || "—"
                    color: Tokens.textSecondary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                    width: parent.width - x - nextLbl.width - Math.round(Tokens.spLg * sf)
                    elide: Text.ElideRight
                }
                Text {
                    id: nextLbl
                    text: "Próxima: " + app.formatDate(card.taskData.next_run_at)
                    color: Tokens.textMuted
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                }
            }
        }
    }

    // ── Subcomponente: formulario nueva tarea ─────────────────────────────
    component TaskForm: Rectangle {
        id: form
        height: visible ? formCol.implicitHeight + Math.round(Tokens.spXl * sf) : 0
        radius: Math.round(Tokens.radiusMd * sf)
        color: Tokens.bgCard
        border.width: 1
        border.color: Tokens.borderSubtle
        clip: true

        required property real sf
        required property var agentsList
        property int preselDay: -1
        signal createTask(var draft)

        // Campos internos
        property string fTitle: ""
        property string fAgentId: agentsList.length > 0 ? (agentsList[0].agent_id || "") : ""
        property string fInstruction: ""
        property string fMode: "recurrent"  // "recurrent" | "oneshot"
        property var fDays: []              // [0..6] días seleccionados (Lun=0)
        property int fHour: 9
        property int fMinute: 0
        property string fRisk: "low"
        property string fNote: ""

        onPreselDayChanged: {
            if (preselDay >= 0 && preselDay <= 6) {
                fDays = [preselDay];
                fMode = "recurrent";
            }
        }

        onVisibleChanged: {
            if (visible && preselDay >= 0 && preselDay <= 6) {
                fDays = [preselDay];
                fMode = "recurrent";
            }
        }

        // Construye el cron y llama createTask
        function submitForm() {
            if (!fTitle || fTitle.trim().length === 0) {
                fNote = "El título es obligatorio";
                return;
            }
            if (!fInstruction || fInstruction.trim().length === 0) {
                fNote = "La instrucción es obligatoria";
                return;
            }

            var minStr = ("0" + fMinute).slice(-2);
            var hrStr = "" + fHour;

            var cron;
            var oneShot;
            if (fMode === "recurrent") {
                var dowPart = fDays.length === 0 ? "*"
                    : fDays.slice().sort(function(a, b) { return a - b; })
                            .map(function(d) { return d === 6 ? 0 : d + 1; })
                            .join(",");
                // Formato 5 campos Unix: min hr dom mon dow (lo que el daemon
                // valida y croniter espera — NO 6 campos con segundos).
                cron = minStr + " " + hrStr + " * * " + dowPart;
                oneShot = false;
            } else {
                // one-shot: cron válido pero one_shot=true (se auto-revoca al primer disparo)
                cron = minStr + " " + hrStr + " * * *";
                oneShot = true;
            }

            fNote = "";
            form.createTask({
                title: fTitle.trim(),
                target_agent_id: fAgentId || null,
                task_instruction: fInstruction.trim(),
                cron: cron,
                one_shot: oneShot,
                risk_ceiling: fRisk
            });
        }

        Column {
            id: formCol
            anchors.fill: parent
            anchors.margins: Math.round(Tokens.spLg * sf)
            spacing: Math.round(Tokens.spMd * sf)

            Text {
                text: "Nueva tarea"
                color: Tokens.textPrimary
                font.family: Tokens.fontDisplay
                font.pixelSize: Math.round(15 * sf)
                font.weight: Font.Medium
            }

            // Título (LumenInput)
            LumenInput {
                id: titleIn
                sf: form.sf
                width: parent.width
                placeholder: "Título de la tarea"
                onTextChanged: form.fTitle = text
            }

            // Selector de agente
            Column {
                width: parent.width
                spacing: Math.round(Tokens.spXs * sf)
                Text {
                    text: "Agente"
                    color: Tokens.textSecondary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                    font.weight: Font.DemiBold
                }
                Flow {
                    width: parent.width
                    spacing: Math.round(Tokens.spSm * sf)

                    // Chip "Cualquiera"
                    Rectangle {
                        height: Math.round(26 * sf)
                        width: anyLbl.width + Math.round(Tokens.spLg * sf)
                        radius: Math.round(Tokens.radiusSm * sf)
                        color: form.fAgentId === "" ? Tokens.accentSubtle : Tokens.bgElevated
                        border.width: 1
                        border.color: form.fAgentId === ""
                                      ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.40)
                                      : Tokens.borderDefault
                        Behavior on color {
                            enabled: !Tokens.reduceMotion
                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                        }
                        Text {
                            id: anyLbl
                            anchors.centerIn: parent
                            text: "Cualquiera"
                            color: form.fAgentId === "" ? Tokens.accentBase : Tokens.textMuted
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(10 * sf)
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: form.fAgentId = ""
                        }
                    }

                    // Chips de agentes
                    Repeater {
                        model: form.agentsList
                        Rectangle {
                            height: Math.round(26 * sf)
                            width: agChipLbl.width + Math.round(Tokens.spLg * sf)
                            radius: Math.round(Tokens.radiusSm * sf)
                            color: form.fAgentId === modelData.agent_id
                                   ? Tokens.accentSubtle : Tokens.bgElevated
                            border.width: 1
                            border.color: form.fAgentId === modelData.agent_id
                                          ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.40)
                                          : Tokens.borderDefault
                            Behavior on color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                            }
                            Text {
                                id: agChipLbl
                                anchors.centerIn: parent
                                text: modelData.name || modelData.agent_id
                                color: form.fAgentId === modelData.agent_id
                                       ? Tokens.accentBase : Tokens.textMuted
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(10 * sf)
                            }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: form.fAgentId = modelData.agent_id
                            }
                        }
                    }
                }
            }

            // Instrucción (TextEdit wrapeado con estilo Sereno)
            Rectangle {
                width: parent.width
                height: Math.round(80 * sf)
                radius: Math.round(Tokens.radiusMd * sf)
                color: instrEdit.activeFocus ? Tokens.bgSunken : Tokens.bgElevated
                border.width: 1
                border.color: instrEdit.activeFocus
                              ? Tokens.accentBase
                              : (instrHover.containsMouse ? Tokens.borderStrong : Tokens.borderDefault)

                Behavior on border.color {
                    enabled: !Tokens.reduceMotion
                    ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                }
                Behavior on color {
                    enabled: !Tokens.reduceMotion
                    ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                }

                // Focus ring
                Rectangle {
                    anchors.fill: parent
                    anchors.margins: -Math.round(2 * sf)
                    radius: Math.round((Tokens.radiusMd + 2) * sf)
                    color: "transparent"
                    border.width: instrEdit.activeFocus ? Math.round(2 * sf) : 0
                    border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.35)
                    visible: instrEdit.activeFocus
                }

                Text {
                    anchors.top: parent.top
                    anchors.topMargin: Math.round(Tokens.spMd * sf)
                    anchors.left: parent.left
                    anchors.leftMargin: Math.round(Tokens.spMd * sf)
                    visible: instrEdit.text.length === 0
                    text: "¿Qué debe hacer el agente?"
                    color: Tokens.textMuted
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(12 * sf)
                }
                TextEdit {
                    id: instrEdit
                    anchors.fill: parent
                    anchors.margins: Math.round(Tokens.spMd * sf)
                    color: Tokens.textPrimary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(12 * sf)
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    clip: true
                    onTextChanged: form.fInstruction = text
                }
                MouseArea {
                    id: instrHover
                    anchors.fill: parent
                    hoverEnabled: true
                    propagateComposedEvents: true
                    onPressed: function(mouse) {
                        instrEdit.forceActiveFocus()
                        mouse.accepted = false
                    }
                }
            }

            // Radio recurrente / una vez
            Row {
                spacing: Math.round(Tokens.spXl * sf)
                Repeater {
                    model: [{ id: "recurrent", label: "Recurrente" }, { id: "oneshot", label: "Una vez" }]
                    Row {
                        spacing: Math.round(Tokens.spSm * sf)
                        Rectangle {
                            width: Math.round(16 * sf); height: Math.round(16 * sf)
                            radius: width / 2
                            color: "transparent"
                            border.width: 2
                            border.color: form.fMode === modelData.id ? Tokens.accentBase : Tokens.borderDefault
                            anchors.verticalCenter: parent.verticalCenter
                            Rectangle {
                                visible: form.fMode === modelData.id
                                width: Math.round(8 * sf); height: width; radius: width / 2
                                color: Tokens.accentBase
                                anchors.centerIn: parent
                            }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: form.fMode = modelData.id
                            }
                        }
                        Text {
                            text: modelData.label
                            color: Tokens.textSecondary
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(12 * sf)
                            anchors.verticalCenter: parent.verticalCenter
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: form.fMode = modelData.id
                            }
                        }
                    }
                }
            }

            // Selector de días (solo visible en modo recurrente)
            Column {
                width: parent.width
                spacing: Math.round(Tokens.spXs * sf)
                visible: form.fMode === "recurrent"

                Text {
                    text: "Días de la semana"
                    color: Tokens.textSecondary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                    font.weight: Font.DemiBold
                }
                Row {
                    spacing: Math.round(Tokens.spXs * sf)
                    Repeater {
                        model: ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
                        Rectangle {
                            property int dayIdx: index
                            property bool selected: {
                                var d = form.fDays;
                                for (var i = 0; i < d.length; i++) { if (d[i] === dayIdx) return true; }
                                return false;
                            }
                            width: Math.round(36 * sf)
                            height: Math.round(28 * sf)
                            radius: Math.round(Tokens.radiusSm * sf)
                            color: selected ? Tokens.accentSubtle : Tokens.bgElevated
                            border.width: 1
                            border.color: selected
                                          ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.40)
                                          : Tokens.borderDefault

                            Behavior on color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                            }

                            Text {
                                anchors.centerIn: parent
                                text: modelData
                                color: selected ? Tokens.accentBase : Tokens.textMuted
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(10 * sf)
                                font.weight: selected ? Font.DemiBold : Font.Normal
                            }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    var d = form.fDays.slice();
                                    var pos = -1;
                                    for (var i = 0; i < d.length; i++) {
                                        if (d[i] === dayIdx) { pos = i; break; }
                                    }
                                    if (pos >= 0) d.splice(pos, 1);
                                    else d.push(dayIdx);
                                    form.fDays = d;
                                }
                            }
                        }
                    }
                }
            }

            // Hora y minuto
            Row {
                spacing: Math.round(Tokens.spSm * sf)
                Text {
                    text: form.fMode === "recurrent" ? "Hora:" : "Hora del disparo:"
                    color: Tokens.textSecondary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                    anchors.verticalCenter: parent.verticalCenter
                }
                // Spinner hora
                Row {
                    spacing: Math.round(Tokens.spXs * sf)
                    Rectangle {
                        width: Math.round(28 * sf); height: Math.round(28 * sf)
                        radius: Math.round(Tokens.radiusSm * sf)
                        color: hourMinusMa.containsMouse ? Tokens.borderDefault : Tokens.bgElevated
                        border.width: 1; border.color: Tokens.borderDefault
                        Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                        Text { anchors.centerIn: parent; text: "−"; color: Tokens.textSecondary; font.family: Tokens.fontBody; font.pixelSize: Math.round(14 * sf) }
                        MouseArea { id: hourMinusMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: form.fHour = Math.max(0, form.fHour - 1) }
                    }
                    Rectangle {
                        width: Math.round(34 * sf); height: Math.round(28 * sf)
                        radius: Math.round(Tokens.radiusSm * sf)
                        color: Tokens.bgSunken
                        border.width: 1; border.color: Tokens.borderSubtle
                        Text { anchors.centerIn: parent; text: ("0" + form.fHour).slice(-2); color: Tokens.textPrimary; font.family: Tokens.fontBody; font.pixelSize: Math.round(13 * sf); font.weight: Font.DemiBold }
                    }
                    Rectangle {
                        width: Math.round(28 * sf); height: Math.round(28 * sf)
                        radius: Math.round(Tokens.radiusSm * sf)
                        color: hourPlusMa.containsMouse ? Tokens.borderDefault : Tokens.bgElevated
                        border.width: 1; border.color: Tokens.borderDefault
                        Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                        Text { anchors.centerIn: parent; text: "+"; color: Tokens.textSecondary; font.family: Tokens.fontBody; font.pixelSize: Math.round(14 * sf) }
                        MouseArea { id: hourPlusMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: form.fHour = Math.min(23, form.fHour + 1) }
                    }
                }
                Text { text: ":"; color: Tokens.textMuted; font.family: Tokens.fontBody; font.pixelSize: Math.round(16 * sf); anchors.verticalCenter: parent.verticalCenter }
                // Spinner minuto
                Row {
                    spacing: Math.round(Tokens.spXs * sf)
                    Rectangle {
                        width: Math.round(28 * sf); height: Math.round(28 * sf)
                        radius: Math.round(Tokens.radiusSm * sf)
                        color: minMinusMa.containsMouse ? Tokens.borderDefault : Tokens.bgElevated
                        border.width: 1; border.color: Tokens.borderDefault
                        Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                        Text { anchors.centerIn: parent; text: "−"; color: Tokens.textSecondary; font.family: Tokens.fontBody; font.pixelSize: Math.round(14 * sf) }
                        MouseArea { id: minMinusMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: form.fMinute = Math.max(0, form.fMinute - 5) }
                    }
                    Rectangle {
                        width: Math.round(34 * sf); height: Math.round(28 * sf)
                        radius: Math.round(Tokens.radiusSm * sf)
                        color: Tokens.bgSunken
                        border.width: 1; border.color: Tokens.borderSubtle
                        Text { anchors.centerIn: parent; text: ("0" + form.fMinute).slice(-2); color: Tokens.textPrimary; font.family: Tokens.fontBody; font.pixelSize: Math.round(13 * sf); font.weight: Font.DemiBold }
                    }
                    Rectangle {
                        width: Math.round(28 * sf); height: Math.round(28 * sf)
                        radius: Math.round(Tokens.radiusSm * sf)
                        color: minPlusMa.containsMouse ? Tokens.borderDefault : Tokens.bgElevated
                        border.width: 1; border.color: Tokens.borderDefault
                        Behavior on color { enabled: !Tokens.reduceMotion; ColorAnimation { duration: Tokens.durFast } }
                        Text { anchors.centerIn: parent; text: "+"; color: Tokens.textSecondary; font.family: Tokens.fontBody; font.pixelSize: Math.round(14 * sf) }
                        MouseArea { id: minPlusMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: form.fMinute = Math.min(55, form.fMinute + 5) }
                    }
                }
            }

            // Risk ceiling
            Row {
                spacing: Math.round(Tokens.spSm * sf)
                Text {
                    text: "Riesgo máximo:"
                    color: Tokens.textSecondary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                    anchors.verticalCenter: parent.verticalCenter
                }
                Repeater {
                    model: [{ id: "low", label: "Bajo" }, { id: "high", label: "Alto" }]
                    Rectangle {
                        height: Math.round(26 * sf)
                        width: riskLbl.width + Math.round(Tokens.spLg * sf)
                        radius: Math.round(Tokens.radiusSm * sf)
                        color: form.fRisk === modelData.id ? Tokens.accentSubtle : Tokens.bgElevated
                        border.width: 1
                        border.color: form.fRisk === modelData.id
                                      ? Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.40)
                                      : Tokens.borderDefault
                        Behavior on color {
                            enabled: !Tokens.reduceMotion
                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                        }
                        Text {
                            id: riskLbl
                            anchors.centerIn: parent
                            text: modelData.label
                            color: form.fRisk === modelData.id ? Tokens.accentBase : Tokens.textMuted
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(10 * sf)
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: form.fRisk = modelData.id
                        }
                    }
                }
            }

            // Error + botón crear
            Row {
                width: parent.width
                spacing: Math.round(Tokens.spMd * sf)
                Text {
                    text: form.fNote
                    visible: form.fNote.length > 0
                    color: Tokens.dangerBase
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width - submitBtn.width - Math.round(Tokens.spMd * sf)
                    elide: Text.ElideRight
                }
                Item {
                    width: form.fNote.length > 0
                           ? 0
                           : parent.width - submitBtn.width - Math.round(Tokens.spMd * sf)
                    height: 1
                }
                LumenButton {
                    id: submitBtn
                    sf: form.sf
                    label: "Crear tarea"
                    variant: "primary"
                    implicitWidth: Math.round(110 * sf)
                    implicitHeight: Math.round(34 * sf)
                    onClicked: form.submitForm()
                }
            }
        }
    }
}
