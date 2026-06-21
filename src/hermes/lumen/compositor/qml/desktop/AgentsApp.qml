import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "." // Tokens singleton — OBLIGATORIO, sin él la pantalla queda en blanco

// ── AgentsApp ─────────────────────────────────────────────────────────────
// Cableado REAL al daemon: ListAgents/GetActiveAgent/CreateAgent/SetActiveAgent/
// DeleteAgent. Un agente = un profile Hermes (personalidad + instrucciones). Las
// skills/MCP se asignan por agente (capability binding). Sin mocks.
Rectangle {
    id: app
    anchors.fill: parent
    color: Tokens.bgSurface
    radius: Math.round(Tokens.radiusLg * sf)
    readonly property real sf: root.sf
    readonly property bool _compact: app.width < Tokens.bpCompact * root.sf

    // ── Agentes ──────────────────────────────────────────────────────────
    property var agents: []
    property string activeId: ""
    property bool loading: true
    property bool creating: false
    property bool busy: false
    property string note: ""
    property var fName: null
    property var fRole: null
    property var fMission: null
    property var fInstr: null
    property string editingId: ""
    // El Cerebro (agente default) tiene system prompt fijo: editar bloquea núcleo.
    property bool editingDefault: app.editingId === "default"

    // ── Capabilities panel state ──────────────────────────────────────────
    property string selectedAgentId: ""

    // Global skills/MCP catalogue (loaded once when any panel opens)
    property var capSkillsCatalogue: []
    property var capMcpCatalogue: []
    property bool capCatalogueLoaded: false

    // Per-agent bindings: reloaded every time the selected agent changes
    property var capBindings: []
    property bool capBindingsLoading: false
    property string capBindingsError: ""

    // In-flight toggle guard
    property var capTogglingSet: ({})

    // ── Composio connections catalogue + per-agent bindings ───────────────
    property var capComposioConnections: []
    property bool capComposioLoaded: false
    property var capComposioBindings: []
    property var capComposioTogglingSet: ({})

    // ── Agent CRUD ────────────────────────────────────────────────────────
    function load() {
        loading = true;
        hermes.call("ag-list", "list_agents", "{}");
        hermes.call("ag-active", "get_active_agent", "{}");
    }
    function create() {
        if (busy) return;
        if (!fName || fName.text.trim().length === 0) { note = "El nombre es obligatorio"; return; }
        busy = true; note = "";
        hermes.call("ag-create", "create_agent", JSON.stringify({ draft_json: {
            name: fName.text.trim(),
            role: fRole ? fRole.text.trim() : "",
            primary_mission: fMission ? fMission.text.trim() : "",
            instructions: fInstr ? fInstr.text.trim() : "",
            language: "es-ES", autonomy_level: "balanced"
        }}));
    }
    function activate(id) { hermes.call("ag-activate", "set_active_agent", JSON.stringify({ agent_id: id })); }
    function del(id) { hermes.call("ag-del", "delete_agent", JSON.stringify({ agent_id: id })); }

    function beginEdit(agentData) {
        app.editingId = agentData.agent_id;
        app.creating = true;
        app.note = "";
        if (app.fName)    app.fName.text    = agentData.name              || "";
        if (app.fRole)    app.fRole.text    = agentData.role              || "";
        if (app.fMission) app.fMission.text = agentData.primary_mission   || "";
        if (app.fInstr)   app.fInstr.text   = agentData.instructions      || "";
        // Do NOT call selectAgent here: the edit form is now a modal overlay,
        // so opening the Capacidades panel simultaneously creates visual clutter.
    }

    function save() {
        if (busy) return;
        if (!fName || fName.text.trim().length === 0) { note = "El nombre es obligatorio"; return; }
        busy = true; note = "";
        if (app.editingId.length > 0) {
            var original = null;
            for (var i = 0; i < app.agents.length; i++) {
                if (app.agents[i].agent_id === app.editingId) { original = app.agents[i]; break; }
            }
            var draft = original ? JSON.parse(JSON.stringify(original)) : {};
            draft["name"]             = fName.text.trim();
            draft["role"]             = fRole    ? fRole.text.trim()    : (draft["role"]             || "");
            draft["primary_mission"]  = fMission ? fMission.text.trim() : (draft["primary_mission"]  || "");
            draft["instructions"]     = fInstr   ? fInstr.text.trim()   : (draft["instructions"]     || "");
            hermes.call("ag-update", "update_agent", JSON.stringify({ agent_id: app.editingId, draft_json: draft }));
        } else {
            hermes.call("ag-create", "create_agent", JSON.stringify({ draft_json: {
                name: fName.text.trim(),
                role: fRole ? fRole.text.trim() : "",
                primary_mission: fMission ? fMission.text.trim() : "",
                instructions: fInstr ? fInstr.text.trim() : "",
                language: "es-ES", autonomy_level: "balanced"
            }}));
        }
    }

    // ── Capabilities helpers ──────────────────────────────────────────────
    function selectAgent(agentId) {
        if (app.selectedAgentId === agentId) {
            app.selectedAgentId = "";
            return;
        }
        app.selectedAgentId = agentId;
        app.capBindingsError = "";
        app._loadCapabilities(agentId);
    }

    function _loadCapabilities(agentId) {
        app.capBindingsLoading = true;
        hermes.call("agcap-list", "list_agent_capabilities",
                    JSON.stringify({ agent_id: agentId }));
        if (!app.capCatalogueLoaded) {
            hermes.call("agcap-skills", "list_skills", "{}");
            hermes.call("agcap-mcps", "list_mcp_servers", "{}");
        }
        hermes.call("agcap-composio", "list_composio_connections", "{}");
        hermes.call("agcap-composio-bindings", "list_agent_composio_connections",
                    JSON.stringify({ agent_id: agentId }));
    }

    function _isBound(kind, capId) {
        for (var i = 0; i < app.capBindings.length; i++) {
            var b = app.capBindings[i];
            if (b.capability_kind === kind && b.capability_id === capId) return true;
        }
        return false;
    }

    function _toggleKey(kind, capId) { return kind + ":" + capId; }

    function toggleSkill(skill) {
        var key = app._toggleKey("skill", skill.package_id);
        if (app.capTogglingSet[key]) return;
        var toggling = app.capTogglingSet;
        toggling[key] = true;
        app.capTogglingSet = toggling;

        if (app._isBound("skill", skill.package_id)) {
            hermes.call("agcap-unbind:skill:" + skill.package_id,
                        "unbind_capability_from_agent",
                        JSON.stringify({
                            agent_id: app.selectedAgentId,
                            capability_kind: "skill",
                            capability_id: skill.package_id
                        }));
        } else {
            hermes.call("agcap-bind:skill:" + skill.package_id,
                        "bind_capability_to_agent",
                        JSON.stringify({
                            agent_id: app.selectedAgentId,
                            capability_kind: "skill",
                            capability_id: skill.package_id,
                            capability_version: skill.version || "1"
                        }));
        }
    }

    function toggleMcp(server) {
        var key = app._toggleKey("mcp", server.server_id);
        if (app.capTogglingSet[key]) return;
        var toggling = app.capTogglingSet;
        toggling[key] = true;
        app.capTogglingSet = toggling;

        if (app._isBound("mcp", server.server_id)) {
            hermes.call("agcap-unbind:mcp:" + server.server_id,
                        "unbind_capability_from_agent",
                        JSON.stringify({
                            agent_id: app.selectedAgentId,
                            capability_kind: "mcp",
                            capability_id: server.server_id
                        }));
        } else {
            hermes.call("agcap-bind:mcp:" + server.server_id,
                        "bind_capability_to_agent",
                        JSON.stringify({
                            agent_id: app.selectedAgentId,
                            capability_kind: "mcp",
                            capability_id: server.server_id,
                            capability_version: "1"
                        }));
        }
    }

    function _isComposioBound(connId) {
        for (var i = 0; i < app.capComposioBindings.length; i++) {
            if (app.capComposioBindings[i] === connId) return true;
        }
        return false;
    }

    function toggleComposio(conn) {
        var cid = conn.id;
        if (app.capComposioTogglingSet[cid]) return;
        var toggling = app.capComposioTogglingSet;
        toggling[cid] = true;
        app.capComposioTogglingSet = toggling;

        if (app._isComposioBound(cid)) {
            hermes.call("agcap-composio-unbind:" + cid,
                        "unbind_composio_connection_from_agent",
                        JSON.stringify({
                            agent_id: app.selectedAgentId,
                            connection_id: cid
                        }));
        } else {
            hermes.call("agcap-composio-bind:" + cid,
                        "bind_composio_connection_to_agent",
                        JSON.stringify({
                            agent_id: app.selectedAgentId,
                            connection_id: cid
                        }));
        }
    }

    // ── Connections ───────────────────────────────────────────────────────
    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "agcap-list") {
                app.capBindingsLoading = false;
                if (!ok) {
                    try { app.capBindingsError = JSON.parse(jsonStr).error || jsonStr; }
                    catch (e) { app.capBindingsError = jsonStr || "Error al cargar capacidades"; }
                    return;
                }
                try { app.capBindings = JSON.parse(jsonStr || "[]"); }
                catch (e) { app.capBindings = []; }
                app.capBindingsError = "";
                return;
            }
            if (reqId === "agcap-skills") {
                try { app.capSkillsCatalogue = ok ? JSON.parse(jsonStr || "[]") : []; }
                catch (e) { app.capSkillsCatalogue = []; }
                if (app.capCatalogueLoaded || app.capMcpCatalogue.length >= 0) {
                    app.capCatalogueLoaded = true;
                }
                return;
            }
            if (reqId === "agcap-mcps") {
                try { app.capMcpCatalogue = ok ? JSON.parse(jsonStr || "[]") : []; }
                catch (e) { app.capMcpCatalogue = []; }
                app.capCatalogueLoaded = true;
                return;
            }
            if (reqId === "agcap-composio") {
                try { app.capComposioConnections = ok ? JSON.parse(jsonStr || "[]") : []; }
                catch (e) { app.capComposioConnections = []; }
                app.capComposioLoaded = true;
                return;
            }
            if (reqId === "agcap-composio-bindings") {
                try { app.capComposioBindings = ok ? JSON.parse(jsonStr || "[]") : []; }
                catch (e) { app.capComposioBindings = []; }
                return;
            }
            if (reqId.indexOf("agcap-composio-bind:") === 0 || reqId.indexOf("agcap-composio-unbind:") === 0) {
                var cpfx = reqId.indexOf("agcap-composio-bind:") === 0 ? "agcap-composio-bind:" : "agcap-composio-unbind:";
                var ccid = reqId.slice(cpfx.length);
                var ctoggling = app.capComposioTogglingSet;
                delete ctoggling[ccid];
                app.capComposioTogglingSet = ctoggling;
                if (!ok) {
                    try { app.capBindingsError = JSON.parse(jsonStr).error || jsonStr; }
                    catch (e) { app.capBindingsError = jsonStr || "Error al cambiar conexión Composio"; }
                    return;
                }
                app.capBindingsError = "";
                if (app.selectedAgentId) {
                    hermes.call("agcap-composio-bindings", "list_agent_composio_connections",
                                JSON.stringify({ agent_id: app.selectedAgentId }));
                }
                return;
            }
            if (reqId.indexOf("agcap-bind:") === 0 || reqId.indexOf("agcap-unbind:") === 0) {
                var parts = reqId.split(":");
                var kind = parts.length >= 2 ? parts[1] : "";
                var capId = parts.length >= 3 ? parts.slice(2).join(":") : "";
                var key = kind + ":" + capId;
                var toggling = app.capTogglingSet;
                delete toggling[key];
                app.capTogglingSet = toggling;

                if (!ok) {
                    try { app.capBindingsError = JSON.parse(jsonStr).error || jsonStr; }
                    catch (e) { app.capBindingsError = jsonStr || "Error al cambiar capacidad"; }
                    return;
                }
                app.capBindingsError = "";
                if (app.selectedAgentId) {
                    hermes.call("agcap-list", "list_agent_capabilities",
                                JSON.stringify({ agent_id: app.selectedAgentId }));
                }
                return;
            }

            if (reqId === "ag-list") {
                app.loading = false;
                try { app.agents = ok ? JSON.parse(jsonStr || "[]") : []; }
                catch (e) { app.agents = []; }
            } else if (reqId === "ag-active") {
                app.activeId = (jsonStr || "").replace(/^"|"$/g, "");
            } else if (reqId === "ag-create") {
                app.busy = false;
                if (ok) {
                    app.creating = false;
                    app.editingId = "";
                    if (app.fName) app.fName.text = "";
                    if (app.fRole) app.fRole.text = "";
                    if (app.fMission) app.fMission.text = "";
                    if (app.fInstr) app.fInstr.text = "";
                    app.load();
                    root.showToast("Agente creado", "success");
                } else {
                    try { app.note = JSON.parse(jsonStr).error || jsonStr; }
                    catch (e) { app.note = jsonStr; }
                }
            } else if (reqId === "ag-update") {
                app.busy = false;
                if (ok) {
                    app.creating = false;
                    app.editingId = "";
                    if (app.fName) app.fName.text = "";
                    if (app.fRole) app.fRole.text = "";
                    if (app.fMission) app.fMission.text = "";
                    if (app.fInstr) app.fInstr.text = "";
                    app.load();
                    root.showToast("Agente actualizado", "success");
                } else {
                    try { app.note = JSON.parse(jsonStr).error || jsonStr; }
                    catch (e) { app.note = jsonStr; }
                }
            } else if (reqId === "ag-activate") {
                app.load();
                if (ok) root.showToast("Agente activado", "success");
                else root.showToast("No se pudo activar el agente", "error");
            } else if (reqId === "ag-del") {
                app.load();
                if (ok) root.showToast("Agente eliminado", "success");
                else { var m = ""; try { m = JSON.parse(jsonStr).error; } catch (e) {} root.showToast(m || "No se pudo eliminar el agente", "error"); }
            }
        }
    }
    Component.onCompleted: load()

    // ── Layout ─────────────────────────────────────────────────────────────
    // Parent Flickable so the entire content (header + form + agent list +
    // each Capacidades panel) scrolls as one unit in a small/centred window.
    Flickable {
        id: mainFlick
        anchors.fill: parent
        anchors.margins: Math.round(Tokens.spXl * sf)
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        contentHeight: mainCol.implicitHeight
        contentWidth: width

        WheelHandler {
            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            onWheel: (event) => {
                mainFlick.contentY = Math.max(
                    0,
                    Math.min(
                        Math.max(0, mainFlick.contentHeight - mainFlick.height),
                        mainFlick.contentY - event.angleDelta.y
                    )
                );
            }
        }

        ScrollBar.vertical: LumenScrollBar { sf: app.sf }

        Column {
            id: mainCol
            // Cap at bpWide; centre horizontally when wider than cap
            width: Math.min(mainFlick.width, Math.round(Tokens.bpWide * sf))
            x: (mainFlick.width - width) / 2
            spacing: Math.round(Tokens.spMd * sf)

            // ── Header row ───────────────────────────────────────────────
            Row {
                width: parent.width

                Column {
                    width: parent.width - newAgentBtn.width - Math.round(Tokens.spMd * sf)
                    spacing: Math.round(Tokens.spXs * sf)

                    Text {
                        text: "Agentes"
                        color: Tokens.textPrimary
                        font.family: Tokens.fontDisplay
                        font.pixelSize: Math.round(18 * sf)
                        font.weight: Font.DemiBold
                    }
                    Text {
                        text: "Cada agente es una personalidad de Hermes con sus instrucciones. Skills, MCP y proveedor se asignan por agente."
                        color: Tokens.textMuted
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(12 * sf)
                        width: parent.width
                        wrapMode: Text.WordWrap
                    }
                }

                LumenButton {
                    id: newAgentBtn
                    sf: app.sf
                    anchors.verticalCenter: parent.verticalCenter
                    label: app.creating ? "Cancelar" : "+ Nuevo agente"
                    variant: app.creating ? "secondary" : "primary"
                    implicitWidth: Math.round(148 * sf)
                    onClicked: { app.creating = !app.creating; app.editingId = ""; app.note = ""; }
                }
            }

            // ── Loading / empty states ────────────────────────────────────
            Text {
                visible: app.loading
                text: "Cargando…"
                color: Tokens.textMuted
                font.family: Tokens.fontBody
                font.pixelSize: Math.round(13 * sf)
            }
            Text {
                visible: !app.loading && app.agents.length === 0 && !app.creating
                text: "No hay agentes. Crea el primero."
                color: Tokens.textMuted
                font.family: Tokens.fontBody
                font.pixelSize: Math.round(13 * sf)
            }

            // ── Agents list ───────────────────────────────────────────────
            ListView {
                id: agentsList
                width: parent.width
                height: contentHeight
                interactive: false
                spacing: Math.round(Tokens.spSm * sf)
                clip: false
                model: app.agents

                delegate: Column {
                    id: agentDelegate
                    width: ListView.view.width
                    spacing: Math.round(Tokens.spXs * sf)

                    property bool isSelected: modelData.agent_id === app.selectedAgentId

                    // Stagger entry appearance
                    opacity: 0.0
                    Component.onCompleted: {
                        if (!Tokens.reduceMotion) {
                            entryAnim.start();   // NumberAnimation no tiene 'delay'; el stagger se omite (delegate visible)
                        } else {
                            opacity = 1.0;
                        }
                    }
                    NumberAnimation {
                        id: entryAnim
                        target: agentDelegate
                        property: "opacity"
                        from: 0.0; to: 1.0
                        duration: Tokens.durBase
                        easing.type: Easing.OutCubic
                    }

                    // ── Agent card ────────────────────────────────────────
                    LumenCard {
                        id: agentCard
                        sf: app.sf
                        pad: 0
                        width: parent.width
                        implicitHeight: agentCardContent.implicitHeight + Math.round(Tokens.spXl * sf)

                        // Active agent gets a success-tinted border
                        Rectangle {
                            anchors.fill: parent
                            radius: Math.round(Tokens.radiusLg * sf)
                            color: "transparent"
                            border.width: modelData.agent_id === app.activeId ? 1 : 0
                            border.color: Tokens.successBase
                        }

                        // Hover tint (gated on reduceMotion)
                        Rectangle {
                            id: cardHoverLayer
                            anchors.fill: parent
                            radius: Math.round(Tokens.radiusLg * sf)
                            color: cardHoverMA.containsMouse ? Qt.rgba(
                                Tokens.bgElevated.r, Tokens.bgElevated.g, Tokens.bgElevated.b, 0.5
                            ) : "transparent"

                            Behavior on color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                            }
                        }
                        MouseArea {
                            id: cardHoverMA
                            anchors.fill: parent
                            hoverEnabled: true
                            propagateComposedEvents: true
                            onClicked: function(mouse) { mouse.accepted = false; }
                        }

                        Item {
                            id: agentCardContent
                            anchors.left: parent.left; anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.leftMargin: Math.round(Tokens.spLg * sf)
                            anchors.rightMargin: Math.round(Tokens.spMd * sf)
                            anchors.topMargin: Math.round(Tokens.spMd * sf)
                            implicitHeight: agentInfoCol.implicitHeight + Math.round(Tokens.spMd * sf)

                            Column {
                                id: agentInfoCol
                                anchors.left: parent.left
                                anchors.right: agentActions.left
                                anchors.top: parent.top
                                anchors.rightMargin: Math.round(Tokens.spSm * sf)
                                spacing: Math.round(Tokens.spXs * sf)

                                Row {
                                    width: parent.width
                                    spacing: Math.round(Tokens.spSm * sf)

                                    // Avatar dot (agent color indicator)
                                    Rectangle {
                                        width: Math.round(10 * sf)
                                        height: width
                                        radius: width / 2
                                        color: modelData.color || Tokens.accentBase
                                        anchors.verticalCenter: parent.verticalCenter
                                    }

                                    // Agent name
                                    Text {
                                        text: modelData.name
                                        color: Tokens.textPrimary
                                        font.family: Tokens.fontDisplay
                                        font.pixelSize: Math.round(14 * sf)
                                        font.weight: Font.DemiBold
                                        anchors.verticalCenter: parent.verticalCenter
                                        elide: Text.ElideRight
                                        // Width bounded so chips don't get pushed off
                                        width: Math.min(implicitWidth, parent.width - Math.round(80 * sf))
                                    }

                                    // "ACTIVO" chip
                                    LumenChip {
                                        visible: modelData.agent_id === app.activeId
                                        sf: app.sf
                                        text: "Activo"
                                        tone: "success"
                                        anchors.verticalCenter: parent.verticalCenter
                                    }

                                    // "Cerebro · omnipotente" chip
                                    LumenChip {
                                        visible: modelData.is_default
                                        sf: app.sf
                                        text: "Cerebro · omnipotente"
                                        tone: "warn"
                                        anchors.verticalCenter: parent.verticalCenter
                                    }
                                }

                                Text {
                                    text: modelData.role || modelData.primary_mission || (modelData.instructions ? modelData.instructions.substring(0, 110) : "Sin instrucciones")
                                    color: Tokens.textMuted
                                    font.family: Tokens.fontBody
                                    font.pixelSize: Math.round(11 * sf)
                                    width: parent.width
                                    elide: Text.ElideRight
                                }
                            }

                            // ── Action buttons ────────────────────────────
                            Row {
                                id: agentActions
                                anchors.right: parent.right
                                anchors.top: parent.top
                                spacing: Math.round(Tokens.spXs * sf)

                                LumenButton {
                                    visible: modelData.agent_id !== app.activeId
                                    sf: app.sf
                                    label: "Activar"
                                    variant: "secondary"
                                    implicitWidth: Math.round(72 * sf)
                                    implicitHeight: Math.round(30 * sf)
                                    onClicked: app.activate(modelData.agent_id)
                                }

                                LumenButton {
                                    sf: app.sf
                                    label: "Editar"
                                    variant: "ghost"
                                    implicitWidth: Math.round(62 * sf)
                                    implicitHeight: Math.round(30 * sf)
                                    onClicked: app.beginEdit(modelData)
                                }

                                // "Capacidades" toggle — amber when open
                                LumenButton {
                                    sf: app.sf
                                    label: "Capacidades"
                                    variant: agentDelegate.isSelected ? "primary" : "secondary"
                                    implicitWidth: Math.round(108 * sf)
                                    implicitHeight: Math.round(30 * sf)
                                    onClicked: app.selectAgent(modelData.agent_id)
                                }

                                LumenButton {
                                    visible: !modelData.is_default
                                    sf: app.sf
                                    label: "Borrar"
                                    variant: "danger"
                                    implicitWidth: Math.round(62 * sf)
                                    implicitHeight: Math.round(30 * sf)
                                    onClicked: app.del(modelData.agent_id)
                                }
                            }
                        }
                    } // agentCard LumenCard

                    // ── Capabilities panel ────────────────────────────────
                    LumenCard {
                        id: capPanel
                        sf: app.sf
                        pad: Tokens.spMd
                        width: parent.width
                        visible: agentDelegate.isSelected
                        implicitHeight: visible ? capPanelCol.implicitHeight + Math.round(Tokens.spXl * sf) : 0
                        // Left indent to visually attach to the card above
                        anchors.leftMargin: Math.round(Tokens.spLg * sf)

                        Behavior on implicitHeight {
                            enabled: !Tokens.reduceMotion
                            NumberAnimation { duration: Tokens.durBase; easing.type: Easing.OutCubic }
                        }

                        Column {
                            id: capPanelCol
                            anchors.fill: parent
                            spacing: Math.round(Tokens.spMd * sf)

                            // Panel header
                            Row {
                                width: parent.width
                                spacing: Math.round(Tokens.spSm * sf)

                                Text {
                                    text: "Capacidades"
                                    color: Tokens.textPrimary
                                    font.family: Tokens.fontDisplay
                                    font.pixelSize: Math.round(13 * sf)
                                    font.weight: Font.DemiBold
                                    anchors.verticalCenter: parent.verticalCenter
                                }

                                // Loading spinner
                                Item {
                                    visible: app.capBindingsLoading
                                    width: Math.round(14 * sf); height: Math.round(14 * sf)
                                    anchors.verticalCenter: parent.verticalCenter

                                    Rectangle {
                                        anchors.fill: parent
                                        radius: width / 2
                                        color: "transparent"
                                        border.width: Math.round(2 * sf)
                                        border.color: Tokens.accentBase

                                        Rectangle {
                                            width: Math.round(5 * sf); height: Math.round(5 * sf)
                                            radius: width / 2
                                            color: Tokens.accentBase
                                            anchors.top: parent.top; anchors.right: parent.right
                                        }

                                        RotationAnimator on rotation {
                                            running: app.capBindingsLoading && !Tokens.reduceMotion
                                            from: 0; to: 360; duration: 900; loops: Animation.Infinite
                                        }
                                    }
                                }
                            }

                            // Error text
                            Text {
                                visible: app.capBindingsError.length > 0
                                text: app.capBindingsError
                                color: Tokens.dangerBase
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(11 * sf)
                                width: parent.width
                                wrapMode: Text.WordWrap
                            }

                            // ── Skills section ────────────────────────────
                            Text {
                                text: "Skills del agente"
                                color: Tokens.textSecondary
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(11 * sf)
                                font.weight: Font.DemiBold
                            }

                            Text {
                                visible: !app.capBindingsLoading && app.capCatalogueLoaded && app.capSkillsCatalogue.length === 0
                                text: "No hay skills globales todavía — instálalas en su app."
                                color: Tokens.textMuted
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(11 * sf)
                                width: parent.width
                                wrapMode: Text.WordWrap
                            }

                            Column {
                                width: parent.width
                                spacing: Math.round(Tokens.spXs * sf)
                                visible: app.capCatalogueLoaded && app.capSkillsCatalogue.length > 0

                                Repeater {
                                    model: app.capSkillsCatalogue

                                    Rectangle {
                                        width: capPanelCol.width
                                        height: Math.round(44 * sf)
                                        radius: Math.round(Tokens.radiusSm * sf)
                                        color: skillCapHover.containsMouse ? Tokens.bgCard : Tokens.bgElevated
                                        border.width: 1
                                        border.color: Tokens.borderSubtle

                                        Behavior on color {
                                            enabled: !Tokens.reduceMotion
                                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                        }

                                        property bool bound: app._isBound("skill", modelData.package_id)
                                        property bool toggling: app.capTogglingSet[app._toggleKey("skill", modelData.package_id)] || false

                                        Row {
                                            anchors.fill: parent
                                            anchors.leftMargin: Math.round(Tokens.spMd * sf)
                                            anchors.rightMargin: Math.round(Tokens.spMd * sf)
                                            spacing: Math.round(Tokens.spMd * sf)

                                            LumenSwitch {
                                                sf: app.sf
                                                checked: parent.parent.bound
                                                anchors.verticalCenter: parent.verticalCenter
                                                opacity: parent.parent.toggling ? 0.5 : 1.0
                                                // Read-only display; toggle handled by MouseArea below
                                            }

                                            Column {
                                                anchors.verticalCenter: parent.verticalCenter
                                                width: parent.width - Math.round(44 * sf) - Math.round(Tokens.spMd * sf)

                                                Text {
                                                    text: modelData.skill_name || modelData.skill_id || "skill"
                                                    color: Tokens.textPrimary
                                                    font.family: Tokens.fontBody
                                                    font.pixelSize: Math.round(12 * sf)
                                                    font.weight: Font.Medium
                                                    elide: Text.ElideRight
                                                    width: parent.width
                                                }
                                                Text {
                                                    text: "v" + (modelData.version || "1") + " · " + (modelData.state || "—")
                                                    color: Tokens.textMuted
                                                    font.family: Tokens.fontBody
                                                    font.pixelSize: Math.round(10 * sf)
                                                }
                                            }
                                        }

                                        MouseArea {
                                            id: skillCapHover
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            enabled: !parent.toggling && agentDelegate.isSelected
                                            onClicked: app.toggleSkill(modelData)
                                        }
                                    }
                                }
                            }

                            // ── MCP section ───────────────────────────────
                            Text {
                                text: "Servidores MCP del agente"
                                color: Tokens.textSecondary
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(11 * sf)
                                font.weight: Font.DemiBold
                            }

                            Text {
                                visible: !app.capBindingsLoading && app.capCatalogueLoaded && app.capMcpCatalogue.length === 0
                                text: "No hay servidores MCP configurados todavía — añádelos en su app."
                                color: Tokens.textMuted
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(11 * sf)
                                width: parent.width
                                wrapMode: Text.WordWrap
                            }

                            Column {
                                width: parent.width
                                spacing: Math.round(Tokens.spXs * sf)
                                visible: app.capCatalogueLoaded && app.capMcpCatalogue.length > 0

                                Repeater {
                                    model: app.capMcpCatalogue

                                    Rectangle {
                                        width: capPanelCol.width
                                        height: Math.round(44 * sf)
                                        radius: Math.round(Tokens.radiusSm * sf)
                                        color: mcpCapHover.containsMouse ? Tokens.bgCard : Tokens.bgElevated
                                        border.width: 1
                                        border.color: Tokens.borderSubtle

                                        Behavior on color {
                                            enabled: !Tokens.reduceMotion
                                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                        }

                                        property bool bound: app._isBound("mcp", modelData.server_id)
                                        property bool toggling: app.capTogglingSet[app._toggleKey("mcp", modelData.server_id)] || false

                                        Row {
                                            anchors.fill: parent
                                            anchors.leftMargin: Math.round(Tokens.spMd * sf)
                                            anchors.rightMargin: Math.round(Tokens.spMd * sf)
                                            spacing: Math.round(Tokens.spMd * sf)

                                            LumenSwitch {
                                                sf: app.sf
                                                checked: parent.parent.bound
                                                anchors.verticalCenter: parent.verticalCenter
                                                opacity: parent.parent.toggling ? 0.5 : 1.0
                                            }

                                            Column {
                                                anchors.verticalCenter: parent.verticalCenter
                                                width: parent.width - Math.round(44 * sf) - Math.round(Tokens.spMd * sf)

                                                Text {
                                                    text: modelData.label || modelData.server_id
                                                    color: Tokens.textPrimary
                                                    font.family: Tokens.fontBody
                                                    font.pixelSize: Math.round(12 * sf)
                                                    font.weight: Font.Medium
                                                    elide: Text.ElideRight
                                                    width: parent.width
                                                }
                                                Text {
                                                    text: (modelData.tool_count || 0) + " tools · " + (modelData.health || "—")
                                                    color: Tokens.textMuted
                                                    font.family: Tokens.fontBody
                                                    font.pixelSize: Math.round(10 * sf)
                                                }
                                            }
                                        }

                                        MouseArea {
                                            id: mcpCapHover
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            enabled: !parent.toggling && agentDelegate.isSelected
                                            onClicked: app.toggleMcp(modelData)
                                        }
                                    }
                                }
                            }

                            // ── Composio Connections section ──────────────
                            Text {
                                text: "Conexiones Composio del agente"
                                color: Tokens.textSecondary
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(11 * sf)
                                font.weight: Font.DemiBold
                            }

                            Text {
                                visible: app.capComposioLoaded && app.capComposioConnections.length === 0
                                text: "Conecta cuentas en Integraciones"
                                color: Tokens.textMuted
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(11 * sf)
                                width: parent.width
                                wrapMode: Text.WordWrap
                            }

                            Column {
                                width: parent.width
                                spacing: Math.round(Tokens.spXs * sf)
                                visible: app.capComposioLoaded && app.capComposioConnections.length > 0

                                Repeater {
                                    model: app.capComposioConnections

                                    Rectangle {
                                        width: capPanelCol.width
                                        height: Math.round(44 * sf)
                                        radius: Math.round(Tokens.radiusSm * sf)
                                        color: composioCapHover.containsMouse ? Tokens.bgCard : Tokens.bgElevated
                                        border.width: 1
                                        border.color: Tokens.borderSubtle

                                        Behavior on color {
                                            enabled: !Tokens.reduceMotion
                                            ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                                        }

                                        property bool bound: app._isComposioBound(modelData.id)
                                        property bool toggling: app.capComposioTogglingSet[modelData.id] || false

                                        Row {
                                            anchors.fill: parent
                                            anchors.leftMargin: Math.round(Tokens.spMd * sf)
                                            anchors.rightMargin: Math.round(Tokens.spMd * sf)
                                            spacing: Math.round(Tokens.spMd * sf)

                                            LumenSwitch {
                                                sf: app.sf
                                                checked: parent.parent.bound
                                                anchors.verticalCenter: parent.verticalCenter
                                                opacity: parent.parent.toggling ? 0.5 : 1.0
                                            }

                                            Column {
                                                anchors.verticalCenter: parent.verticalCenter
                                                width: parent.width - Math.round(44 * sf) - Math.round(Tokens.spMd * sf)

                                                Text {
                                                    text: (modelData.alias && modelData.alias.length > 0)
                                                          ? modelData.alias
                                                          : (modelData.toolkit_slug + " · " + (modelData.id ? modelData.id.slice(0, 6) : "?"))
                                                    color: Tokens.textPrimary
                                                    font.family: Tokens.fontBody
                                                    font.pixelSize: Math.round(12 * sf)
                                                    font.weight: Font.Medium
                                                    elide: Text.ElideRight
                                                    width: parent.width
                                                }
                                                Text {
                                                    text: modelData.toolkit_slug + " · " + (modelData.status || "—")
                                                    color: Tokens.textMuted
                                                    font.family: Tokens.fontBody
                                                    font.pixelSize: Math.round(10 * sf)
                                                }
                                            }
                                        }

                                        MouseArea {
                                            id: composioCapHover
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            enabled: !parent.toggling && agentDelegate.isSelected
                                            onClicked: app.toggleComposio(modelData)
                                        }
                                    }
                                }
                            }

                        } // capPanelCol
                    } // capPanel LumenCard

                } // agentDelegate Column
            } // ListView

        } // mainCol Column
    } // mainFlick Flickable

    // ── Create / Edit form modal ───────────────────────────────────────────
    // Declared LAST so it sits above the Flickable in paint order.
    // LumenModal carries z: Tokens.zModal — no inline z override needed.
    // Closing via scrim click or Cancelar sets app.creating = false.
    LumenModal {
        id: agentFormModal
        sf: app.sf
        open: app.creating
        onClosed: { app.creating = false; app.editingId = ""; app.note = ""; }

        Column {
            width: parent.width
            spacing: Math.round(Tokens.spSm * sf)

            // Modal title row
            Row {
                width: parent.width
                spacing: Math.round(Tokens.spSm * sf)

                Text {
                    text: app.editingId.length > 0 ? "Editar agente" : "Nuevo agente"
                    color: Tokens.textPrimary
                    font.family: Tokens.fontDisplay
                    font.pixelSize: Math.round(16 * sf)
                    font.weight: Font.DemiBold
                    // fill remaining width so the X button stays right-aligned
                    width: parent.width - closeFormBtn.implicitWidth - Math.round(Tokens.spSm * sf)
                    elide: Text.ElideRight
                }

                LumenButton {
                    id: closeFormBtn
                    sf: app.sf
                    label: "✕"
                    variant: "ghost"
                    implicitWidth: Math.round(32 * sf)
                    implicitHeight: Math.round(32 * sf)
                    onClicked: agentFormModal.closed()
                }
            }

            // Cerebro notice
            Rectangle {
                visible: app.editingDefault
                width: parent.width
                height: visible ? cerebroNotice.implicitHeight + Math.round(Tokens.spSm * sf) : 0
                radius: Math.round(Tokens.radiusSm * sf)
                color: Tokens.warnSubtle
                border.width: 1
                border.color: Qt.rgba(Tokens.warnBase.r, Tokens.warnBase.g, Tokens.warnBase.b, 0.25)

                Text {
                    id: cerebroNotice
                    anchors.left: parent.left; anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.leftMargin: Math.round(Tokens.spSm * sf)
                    anchors.rightMargin: Math.round(Tokens.spSm * sf)
                    text: "Cerebro · omnipotente. Su system prompt es fijo para que el SO siempre funcione. Aquí solo ajustas su personalidad y tono."
                    color: Tokens.warnBase
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                    wrapMode: Text.WordWrap
                }
            }

            // Nombre
            LumenInput {
                sf: app.sf
                width: parent.width
                placeholder: "Nombre del agente"
                Component.onCompleted: app.fName = this
                enabled: !app.editingDefault
            }

            // Rol — oculto al editar el Cerebro
            LumenInput {
                sf: app.sf
                width: parent.width
                placeholder: "Rol (ej. Diseñador web)"
                visible: !app.editingDefault
                height: visible ? implicitHeight : 0
                Component.onCompleted: app.fRole = this
            }

            // Misión — oculto al editar el Cerebro
            LumenInput {
                sf: app.sf
                width: parent.width
                placeholder: "Misión principal"
                visible: !app.editingDefault
                height: visible ? implicitHeight : 0
                Component.onCompleted: app.fMission = this
            }

            // Instrucciones / personalidad (multiline)
            Rectangle {
                id: instrBox
                width: parent.width
                height: Math.round(90 * sf)
                radius: Math.round(Tokens.radiusMd * sf)
                color: instrArea.activeFocus ? Tokens.bgSunken : Tokens.bgElevated
                border.width: 1
                border.color: {
                    if (instrArea.activeFocus) return Tokens.accentBase;
                    if (instrHover.containsMouse) return Tokens.borderStrong;
                    return Tokens.borderDefault;
                }

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
                    border.width: instrArea.activeFocus ? Math.round(2 * sf) : 0
                    border.color: Qt.rgba(Tokens.accentBase.r, Tokens.accentBase.g, Tokens.accentBase.b, 0.35)
                    visible: instrArea.activeFocus
                }

                TextEdit {
                    id: instrArea
                    anchors.fill: parent
                    anchors.margins: Math.round(Tokens.spMd * sf)
                    font.pixelSize: Math.round(13 * sf)
                    font.family: Tokens.fontBody
                    color: Tokens.textPrimary
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    clip: true
                    Component.onCompleted: app.fInstr = this

                    Text {
                        anchors.fill: parent
                        visible: instrArea.text.length === 0
                        text: app.editingDefault
                              ? "Personalidad / tono extra (se suma al cerebro)"
                              : "Instrucciones / personalidad"
                        color: Tokens.textMuted
                        font.pixelSize: Math.round(13 * sf)
                        font.family: Tokens.fontBody
                    }
                }

                MouseArea {
                    id: instrHover
                    anchors.fill: parent
                    hoverEnabled: true
                    propagateComposedEvents: true
                    onPressed: function(mouse) {
                        instrArea.forceActiveFocus();
                        mouse.accepted = false;
                    }
                }
            }

            // Note + save row
            Row {
                width: parent.width
                spacing: Math.round(Tokens.spSm * sf)

                Text {
                    text: app.note
                    color: Tokens.dangerBase
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width - saveBtn.implicitWidth - Math.round(Tokens.spSm * sf)
                    elide: Text.ElideRight
                    wrapMode: Text.WordWrap
                }

                LumenButton {
                    id: saveBtn
                    sf: app.sf
                    loading: app.busy
                    label: app.busy
                           ? (app.editingId.length > 0 ? "Guardando…" : "Creando…")
                           : (app.editingId.length > 0 ? "Guardar cambios" : "Crear agente")
                    variant: "primary"
                    implicitWidth: Math.round(152 * sf)
                    onClicked: app.save()
                }
            }
        }
    }
}
