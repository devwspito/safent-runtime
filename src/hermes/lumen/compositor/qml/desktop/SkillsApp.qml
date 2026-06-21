import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// ── SkillsApp ─────────────────────────────────────────────────────────────
// Lista + gobernanza de skills cableada al daemon (D-Bus): ListSkills /
// PromoteSkill (VALIDATED→AUTONOMOUS). ENSEÑANZA por demostración cableada al
// training REAL del shell-server (PipeWire screen + voz Whisper + clicks/keys →
// SKILL.md firmada): POST :7517/api/v1/training → /start → /stop. El backend
// existe (src/hermes/shell_server/training + screen_capture). (Migrar el
// training a D-Bus nativo es follow-up; aquí se reconecta lo que YA funciona.)
Item {
    id: app
    anchors.fill: parent

    readonly property real sf: root.sf
    readonly property bool _compact: app.width < Tokens.bpCompact * root.sf
    readonly property string trainBase: "http://127.0.0.1:7517/api/v1/training"

    property var    skills: []
    property bool   loading: true
    property bool   teaching: false
    property string recSession: ""
    property string recSkillName: ""
    property bool   recBusy: false
    property string note: ""
    property var    fSkill: null
    property var    fDesc: null

    // Sync teaching state into root so the global overlay can react.
    onRecSessionChanged: {
        root.activeTeachingSession = recSession;
        root.activeTeachingSkillName = recSession.length > 0 ? app.recSkillName : "";
    }
    onRecSkillNameChanged: {
        if (recSession.length > 0) root.activeTeachingSkillName = recSkillName;
    }

    // El stop+sign lo gestiona el root (funciona aunque SkillsApp se desmonte).
    Connections {
        target: root
        function onTeachingSignedOk() {
            app.recBusy = false; app.recSession = ""; app.recSkillName = "";
            app.teaching = false;
            if (app.fSkill) app.fSkill.text = "";
            if (app.fDesc)  app.fDesc.text  = "";
            app.note = ""; app.load();
        }
        function onTeachingSignFailed() {
            app.recBusy = false;
            app.note = "No se pudo guardar la skill. Reinténtalo.";
        }
    }

    function load() { loading = true; hermes.call("sk-list", "list_skills", "{}"); }
    function promote(pid) { hermes.call("sk-promote", "promote_skill", JSON.stringify({ package_id: pid })); }

    // ── Skill Hub ──
    property var    hubResults: []
    property var    hubInstalled: []
    property bool   hubSearching: false
    property string hubNote: ""
    property string hubOpId: ""
    property string hubBusyId: ""
    property var    hubQ: null
    property string hubPendingReqId: ""
    property string hubCurrentQueryId: ""

    Timer {
        id: hubDebounceTimer
        interval: 300; repeat: false; running: false
        onTriggered: app._hubFireSearch()
    }
    Timer {
        id: hubOpTimer
        interval: 2500; repeat: true; running: false
        onTriggered: hermes.call("hub-op", "get_hub_op_status", JSON.stringify({ op_id: app.hubOpId }))
    }

    function hubLoad() { hermes.call("hub-installed", "list_hub_skills", "{}"); }

    function hubSearchDebounced() {
        var q = hubQ ? hubQ.text.trim() : "";
        if (!q) {
            hubDebounceTimer.stop();
            if (app.hubCurrentQueryId) {
                hermes.call("hub-cancel", "cancel_skills_hub_search",
                            JSON.stringify({ query_id: app.hubCurrentQueryId }));
                app.hubCurrentQueryId = "";
            }
            app.hubSearching = false;
            app.hubResults   = [];
            app.hubNote      = "";
            return;
        }
        hubDebounceTimer.restart();
    }

    function _hubFireSearch() {
        var q = hubQ ? hubQ.text.trim() : "";
        if (!q) return;
        if (app.hubCurrentQueryId) {
            hermes.call("hub-cancel", "cancel_skills_hub_search",
                        JSON.stringify({ query_id: app.hubCurrentQueryId }));
            app.hubCurrentQueryId = "";
        }
        var reqId = "hub-search-" + Date.now();
        app.hubPendingReqId = reqId;
        app.hubSearching    = true;
        app.hubNote         = "";
        hermes.call(reqId, "search_skills_hub",
                    JSON.stringify({ query: q, source: "all", limit: 20 }));
    }

    function hubSearch() {
        hubDebounceTimer.stop();
        app._hubFireSearch();
    }

    function hubInstall(r) {
        if (hubOpId.length > 0 || hubBusyId.length > 0) return;
        hubBusyId = r.identifier;
        hubNote   = "Centro de Seguridad: analizando " + r.name + "…";
        root.beginGatedInstall(
            { kind: "skill", identifier: r.identifier },
            "install_hub_skill",
            "hub-install",
            { identifier: r.identifier }
        );
    }

    Connections {
        target: root
        function onInstallResolved(reqId) {
            if (reqId === "hub-install") { app.hubBusyId = ""; app.hubNote = ""; }
        }
    }

    function hubIsInstalled(nm) {
        for (var i = 0; i < hubInstalled.length; i++) if (hubInstalled[i].name === nm) return true;
        return false;
    }

    function startTeaching() {
        if (!fSkill || fSkill.text.trim().length === 0) { note = "Pon un nombre a la skill"; return; }
        recBusy = true; note = "Creando sesión…";
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE) return;
            if (xhr.status >= 200 && xhr.status < 300) {
                try { var s = JSON.parse(xhr.responseText); app._beginRecording(s.session_id); }
                catch (e) { app.recBusy = false; app.note = "Respuesta inesperada del training"; }
            } else { app.recBusy = false; app.note = "No se pudo iniciar (¿shell-server activo?) — HTTP " + xhr.status; }
        };
        xhr.open("POST", trainBase);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.send(JSON.stringify({ skill_name: fSkill.text.trim(), description: fDesc ? fDesc.text.trim() : "", surface_kind: "browser" }));
    }

    function _beginRecording(sid) {
        var capturedName = fSkill ? fSkill.text.trim() : "";
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE) return;
            app.recBusy = false;
            if (xhr.status >= 200 && xhr.status < 300) {
                app.recSkillName = capturedName;
                app.recSession   = sid;
                app.note = "● Grabando — demuestra la tarea; narra en voz alta.";
            } else { app.note = "No se pudo grabar — HTTP " + xhr.status; }
        };
        xhr.open("POST", trainBase + "/" + sid + "/start");
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.send("{}");
    }

    function stopTeaching() {
        if (!recSession) return;
        recBusy = true; note = "Compilando y firmando skill…";
        root.stopAndSignTeaching();
    }

    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "sk-list") {
                app.loading = false;
                try { app.skills = ok ? JSON.parse(jsonStr || "[]") : []; } catch (e) { app.skills = []; }
            } else if (reqId === "sk-promote") {
                app.load(); root.showToast("Skill promovida a autónoma", "success");
            } else if (reqId.indexOf("hub-search-") === 0) {
                if (reqId !== app.hubPendingReqId) return;
                app.hubSearching = false;
                if (!ok) { app.hubNote = "✕ Error al buscar."; return; }
                try {
                    var payload = JSON.parse(jsonStr || "{}");
                    if (payload.cancelled) return;
                    app.hubCurrentQueryId = "";
                    var res = Array.isArray(payload.results) ? payload.results
                              : (Array.isArray(payload) ? payload : []);
                    app.hubResults = res;
                    if (res.length === 0) app.hubNote = "Sin resultados (o sin red).";
                    else app.hubNote = res.length + " resultado" + (res.length === 1 ? "" : "s");
                } catch (e) { app.hubResults = []; app.hubNote = "Sin resultados."; }
            } else if (reqId === "hub-cancel") {
                // fire-and-forget
            } else if (reqId === "hub-installed") {
                try { app.hubInstalled = ok ? JSON.parse(jsonStr || "[]") : []; } catch (e) { app.hubInstalled = []; }
            } else if (reqId === "hub-install") {
                try {
                    var ho = JSON.parse(jsonStr || "{}");
                    if (ok && ho.op_id) { app.hubOpId = ho.op_id; hubOpTimer.start(); }
                    else { app.hubBusyId = ""; app.hubNote = "✕ " + (ho.error || jsonStr); }
                } catch (e) { app.hubBusyId = ""; app.hubNote = "✕ " + jsonStr; }
            } else if (reqId === "hub-op") {
                try {
                    var st = JSON.parse(jsonStr || "{}");
                    if (st.status === "done") {
                        hubOpTimer.stop(); app.hubOpId = ""; app.hubBusyId = "";
                        app.hubNote = "✓ Skill instalada — el agente ya puede usarla";
                        root.showToast("Skill del hub instalada", "success"); app.hubLoad();
                    } else if (st.status === "error" || st.status === "unknown") {
                        hubOpTimer.stop(); app.hubOpId = ""; app.hubBusyId = "";
                        app.hubNote = "✕ " + (st.error_message || "instalación falló");
                    }
                } catch (e) { /* sigue sondeando */ }
            }
        }
    }

    Component.onCompleted: { load(); hubLoad(); }

    // ── Skill state → LumenChip tone ──
    function stateTone(s) {
        s = (s || "").toLowerCase();
        if (s.indexOf("autonom") >= 0) return "success";
        if (s.indexOf("valid")   >= 0) return "info";
        if (s.indexOf("deprec")  >= 0) return "danger";
        return "neutral";
    }

    // ── Layout ──
    Rectangle {
        anchors.fill: parent
        color:        Tokens.bgSurface
        radius:       Math.round(Tokens.radiusLg * sf)
    }

    Flickable {
        id: pageFlick
        anchors {
            fill:         parent
            margins:      Math.round(Tokens.spXl * sf)
        }
        contentWidth:  width
        contentHeight: pageCol.implicitHeight
        clip:          true
        boundsBehavior: Flickable.StopAtBounds

        WheelHandler {
            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            onWheel: (event) => {
                pageFlick.contentY = Math.max(0,
                    Math.min(Math.max(0, pageFlick.contentHeight - pageFlick.height),
                             pageFlick.contentY - event.angleDelta.y));
            }
        }

        ScrollBar.vertical: LumenScrollBar { sf: app.sf }

        Column {
            id: pageCol
            // Cap at bpWide; centre horizontally on very wide windows
            width:   Math.min(pageFlick.width, Math.round(Tokens.bpWide * sf))
            x:       (pageFlick.width - width) / 2
            spacing: Math.round(Tokens.spLg * sf)

            // ── Header ──
            RowLayout {
                width: parent.width
                spacing: Math.round(Tokens.spMd * sf)

                Column {
                    Layout.fillWidth: true
                    spacing: Math.round(Tokens.spXs * sf)

                    Text {
                        text:            "Skills"
                        font.family:     Tokens.fontDisplay
                        font.pixelSize:  Math.round(20 * sf)
                        font.weight:     Font.Medium
                        color:           Tokens.textPrimary
                        font.letterSpacing:   -0.3
                    }
                    Text {
                        text: "Capacidades reutilizables. Enséñale una nueva por demostración: graba la pantalla y narra; Hermes la compila y firma."
                        font.family:    Tokens.fontBody
                        font.pixelSize: Math.round(13 * sf)
                        color:          Tokens.textMuted
                        width:          parent.width
                        wrapMode:       Text.WordWrap
                    }
                }

                // "+ Enseñar skill" button — preserved wiring: toggles app.teaching
                LumenButton {
                    id: teachBtn
                    sf:      app.sf
                    label:   app.teaching ? "Cancelar" : "+ Enseñar skill"
                    variant: app.teaching ? "secondary" : "primary"
                    implicitWidth: Math.round(148 * sf)
                    Layout.alignment: Qt.AlignVCenter
                    onClicked: {
                        if (app.recSession) return;
                        app.teaching = !app.teaching;
                        app.note     = "";
                    }
                }
            }

            // ── Teaching panel ──
            LumenCard {
                id: teachPanel
                sf:      app.sf
                pad:     Tokens.spMd
                width:   parent.width
                visible: app.teaching

                implicitHeight: visible
                    ? Math.round(Tokens.spMd * sf) * 2 + teachCol.implicitHeight
                    : 0

                // Accent border while recording
                Rectangle {
                    anchors.fill:   parent
                    radius:         Math.round(Tokens.radiusLg * sf)
                    color:          "transparent"
                    border.width:   1
                    border.color:   app.recSession ? Tokens.dangerBase : Tokens.borderDefault
                    z:              1
                }

                Column {
                    id: teachCol
                    width:   parent.width
                    spacing: Math.round(Tokens.spMd * sf)

                    // Skill name input
                    LumenInput {
                        id:          nameIn
                        sf:          app.sf
                        width:       parent.width
                        placeholder: "Nombre de la skill (ej. Subir factura a Holded)"
                        enabled:     !app.recSession
                        Component.onCompleted: app.fSkill = nameIn
                    }

                    // Description input — multi-line via TextEdit inside a styled box
                    Rectangle {
                        width:        parent.width
                        height:       Math.round(64 * sf)
                        radius:       Math.round(Tokens.radiusMd * sf)
                        color:        descIn.activeFocus ? Tokens.bgSunken : Tokens.bgElevated
                        border.width: 1
                        border.color: descIn.activeFocus ? Tokens.accentBase : Tokens.borderDefault

                        Behavior on border.color {
                            enabled: !Tokens.reduceMotion
                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                        }

                        TextEdit {
                            id: descIn
                            anchors {
                                fill:    parent
                                margins: Math.round(Tokens.spMd * sf)
                            }
                            font.family:    Tokens.fontBody
                            font.pixelSize: Math.round(13 * sf)
                            color:          Tokens.textPrimary
                            wrapMode:       TextEdit.Wrap
                            clip:           true
                            selectByMouse:  true
                            enabled:        !app.recSession
                            Component.onCompleted: app.fDesc = descIn

                            Text {
                                anchors.top:  parent.top
                                anchors.left: parent.left
                                visible:      descIn.text.length === 0
                                text:         "Qué hace (opcional)"
                                font.family:  Tokens.fontBody
                                font.pixelSize: Math.round(13 * sf)
                                color:        Tokens.textMuted
                            }
                        }
                    }

                    // Privacy note (only when not recording)
                    Text {
                        width:          parent.width
                        visible:        !app.recSession
                        wrapMode:       Text.WordWrap
                        text:           "Mientras grabes, Hermes registrará tu pantalla y tu voz. Los clicks y teclas no se capturan solos, así que narra en voz alta lo que vas haciendo (\"abro Holded, subo la factura desde Descargas…\")."
                        font.family:    Tokens.fontBody
                        font.pixelSize: Math.round(11 * sf)
                        color:          Tokens.textMuted
                    }

                    // Note + record button row
                    RowLayout {
                        width: parent.width
                        spacing: Math.round(Tokens.spMd * sf)

                        Text {
                            Layout.fillWidth: true
                            text:           app.note
                            font.family:    Tokens.fontBody
                            font.pixelSize: Math.round(11 * sf)
                            // "●" prefix = recording indicator → dangerBase
                            color: app.note.indexOf("●") === 0 ? Tokens.dangerBase : Tokens.textSecondary
                            elide: Text.ElideRight
                            wrapMode: Text.WordWrap
                            Layout.alignment: Qt.AlignVCenter
                        }

                        // Record / Stop button — preserved wiring: startTeaching / stopTeaching
                        LumenButton {
                            sf:      app.sf
                            label:   app.recBusy ? "…" : (app.recSession ? "Detener y guardar" : "Empezar a grabar")
                            variant: app.recSession ? "danger" : "primary"
                            loading: app.recBusy
                            implicitWidth: Math.round(176 * sf)
                            Layout.alignment: Qt.AlignVCenter
                            onClicked: app.recSession ? app.stopTeaching() : app.startTeaching()
                        }
                    }
                }
            }

            // ── Loading / empty ──
            Text {
                visible:        app.loading
                text:           "Cargando…"
                font.family:    Tokens.fontBody
                font.pixelSize: Math.round(13 * sf)
                color:          Tokens.textMuted
            }

            Text {
                visible:        !app.loading && app.skills.length === 0 && !app.teaching
                text:           "Aún no hay skills. Pulsa «+ Enseñar skill» y demuéstrale una tarea."
                font.family:    Tokens.fontBody
                font.pixelSize: Math.round(13 * sf)
                color:          Tokens.textMuted
                width:          parent.width
                wrapMode:       Text.WordWrap
            }

            // ── Skill Hub ──
            LumenCard {
                sf:    app.sf
                pad:   Tokens.spMd
                width: parent.width
                implicitHeight: Math.round(Tokens.spMd * sf) * 2 + hubCol.implicitHeight

                Column {
                    id: hubCol
                    width:   parent.width
                    spacing: Math.round(Tokens.spSm * sf)

                    // Search row
                    RowLayout {
                        width: parent.width
                        spacing: Math.round(Tokens.spSm * sf)

                        LumenInput {
                            id:          hubQin
                            sf:          app.sf
                            Layout.fillWidth: true
                            placeholder: "Buscar en el Skill Hub de Hermes (ej. email, calendar, github)…"
                            onAccepted:  app.hubSearch()
                            Component.onCompleted: {
                                app.hubQ = hubQin;
                                // wire textChanged to debounce
                            }
                        }

                        // Internal wiring: watch hubQin.text changes via binding
                        Connections {
                            target: hubQin
                            function onTextChanged() { app.hubSearchDebounced(); }
                        }

                        LumenButton {
                            sf:      app.sf
                            label:   "Buscar"
                            variant: "secondary"
                            implicitWidth: Math.round(80 * sf)
                            loading: app.hubSearching
                            onClicked: app.hubSearch()
                        }
                    }

                    // Hub status note
                    Text {
                        visible:        app.hubNote.length > 0
                        text:           app.hubNote
                        font.family:    Tokens.fontBody
                        font.pixelSize: Math.round(11 * sf)
                        color: {
                            if (app.hubNote.indexOf("✕") === 0) return Tokens.dangerBase;
                            if (app.hubNote.indexOf("✓") === 0) return Tokens.successBase;
                            return Tokens.textSecondary;
                        }
                        width:    parent.width
                        wrapMode: Text.WordWrap
                    }

                    // Hub results list
                    ListView {
                        id: hubResultsList
                        visible:       app.hubResults.length > 0
                        width:         parent.width
                        height:        Math.min(Math.round(180 * sf), contentHeight)
                        spacing:       Math.round(Tokens.spXs * sf)
                        clip:          true
                        model:         app.hubResults
                        boundsBehavior: Flickable.StopAtBounds

                        WheelHandler {
                            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                            onWheel: (event) => {
                                hubResultsList.contentY = Math.max(0,
                                    Math.min(Math.max(0, hubResultsList.contentHeight - hubResultsList.height),
                                             hubResultsList.contentY - event.angleDelta.y));
                            }
                        }

                        ScrollBar.vertical: LumenScrollBar { sf: app.sf }

                        delegate: Rectangle {
                            width:  ListView.view.width
                            height: Math.round(48 * sf)
                            radius: Math.round(Tokens.radiusSm * sf)
                            color:  Tokens.bgElevated
                            border.width: 1
                            border.color: Tokens.borderSubtle

                            RowLayout {
                                anchors {
                                    fill:         parent
                                    leftMargin:   Math.round(Tokens.spMd * sf)
                                    rightMargin:  Math.round(Tokens.spMd * sf)
                                }
                                spacing: Math.round(Tokens.spSm * sf)

                                Column {
                                    Layout.fillWidth: true
                                    spacing: Math.round(2 * sf)

                                    Text {
                                        text:           modelData.name + "  ·  " + (modelData.source || "")
                                        font.family:    Tokens.fontBody
                                        font.pixelSize: Math.round(12 * sf)
                                        font.weight:    Font.Medium
                                        color:          Tokens.textPrimary
                                        elide:          Text.ElideRight
                                        width:          parent.width
                                    }
                                    Text {
                                        text:           modelData.description || ""
                                        font.family:    Tokens.fontBody
                                        font.pixelSize: Math.round(11 * sf)
                                        color:          Tokens.textMuted
                                        elide:          Text.ElideRight
                                        width:          parent.width
                                    }
                                }

                                LumenButton {
                                    sf:      app.sf
                                    label:   app.hubBusyId === modelData.identifier ? "…"
                                             : (app.hubIsInstalled(modelData.name) ? "Instalada" : "Instalar")
                                    variant: app.hubIsInstalled(modelData.name) ? "secondary" : "primary"
                                    enabled: !app.hubIsInstalled(modelData.name)
                                    loading: app.hubBusyId === modelData.identifier
                                    implicitWidth: Math.round(84 * sf)
                                    Layout.alignment: Qt.AlignVCenter
                                    onClicked: app.hubInstall(modelData)
                                }
                            }
                        }
                    }
                }
            }

            // ── Installed skills list ──
            ListView {
                id: skillsList
                width:          parent.width
                height:         Math.min(
                                    Math.round(400 * sf),
                                    contentHeight
                                )
                spacing:        Math.round(Tokens.spSm * sf)
                clip:           true
                model:          app.skills
                boundsBehavior: Flickable.StopAtBounds

                ScrollBar.vertical: LumenScrollBar { sf: app.sf }

                WheelHandler {
                    acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                    onWheel: (event) => {
                        skillsList.contentY = Math.max(0,
                            Math.min(Math.max(0, skillsList.contentHeight - skillsList.height),
                                     skillsList.contentY - event.angleDelta.y));
                    }
                }

                delegate: Rectangle {
                    width:        ListView.view.width
                    height:       Math.round(60 * sf)
                    radius:       Math.round(Tokens.radiusMd * sf)
                    color:        Tokens.bgCard
                    border.width: 1
                    border.color: Tokens.borderSubtle

                    RowLayout {
                        anchors {
                            fill:         parent
                            leftMargin:   Math.round(Tokens.spLg * sf)
                            rightMargin:  Math.round(Tokens.spMd * sf)
                        }
                        spacing: Math.round(Tokens.spMd * sf)

                        // State dot
                        Rectangle {
                            width:  Math.round(8 * sf)
                            height: width
                            radius: width / 2
                            Layout.alignment: Qt.AlignVCenter
                            color: {
                                var tone = app.stateTone(modelData.state);
                                if (tone === "success") return Tokens.successBase;
                                if (tone === "info")    return Tokens.infoBase;
                                if (tone === "danger")  return Tokens.dangerBase;
                                return Tokens.textMuted;
                            }
                        }

                        Column {
                            Layout.fillWidth: true
                            spacing: Math.round(2 * sf)

                            Text {
                                text:           modelData.skill_name || modelData.skill_id || "skill"
                                font.family:    Tokens.fontBody
                                font.pixelSize: Math.round(14 * sf)
                                font.weight:    Font.Medium
                                color:          Tokens.textPrimary
                                elide:          Text.ElideRight
                                width:          parent.width
                            }

                            RowLayout {
                                spacing: Math.round(Tokens.spSm * sf)

                                LumenChip {
                                    sf:   app.sf
                                    text: modelData.state || "—"
                                    tone: app.stateTone(modelData.state)
                                }

                                Text {
                                    text:           "v" + (modelData.version || "1") + (modelData.surface_kinds ? " · " + modelData.surface_kinds : "")
                                    font.family:    Tokens.fontBody
                                    font.pixelSize: Math.round(11 * sf)
                                    color:          Tokens.textMuted
                                    elide:          Text.ElideRight
                                }
                            }
                        }

                        // Promote button — preserved wiring: promote(package_id)
                        LumenButton {
                            sf:      app.sf
                            label:   "Promover"
                            variant: "ghost"
                            implicitWidth: Math.round(90 * sf)
                            visible: (modelData.state || "").toLowerCase().indexOf("valid") >= 0
                            Layout.alignment: Qt.AlignVCenter
                            onClicked: app.promote(modelData.package_id)
                        }
                    }
                }
            }

            // Bottom spacer so last item clears the scrollbar gutter
            Item { width: 1; height: Math.round(Tokens.spXl * sf) }
        }
    }
}
