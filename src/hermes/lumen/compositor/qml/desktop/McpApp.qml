import QtQuick
import QtQuick.Controls
import "."

// ── McpApp ────────────────────────────────────────────────────────────────
// Servidores MCP REALES vía daemon (D-Bus): ListMcpServers/AddMcpServer/
// RemoveMcpServer. El catálogo curado instala con un click (argv real, runner
// allowlist npx/uvx/node en el daemon); la lista muestra salud + nº de tools
// del McpServerManager. Las tools entran al LLM por mcp_tool_specs → broker
// (HITL forzado para USER_ADDED — el Centro de Seguridad gobierna cada call).
Rectangle {
    id: app
    anchors.fill: parent
    color: Tokens.bgSurface
    radius: Math.round(Tokens.radiusLg * sf)
    readonly property real sf: root.sf

    // Catálogo curado: server_id (patrón ServerSlug) + argv ejecutable real.
    // envSchema (opcional): campos BYOK que el usuario debe rellenar antes de
    // instalar. El diálogo de configuración aparece automáticamente cuando el
    // servidor lo declara. Claves permitidas: OD_DAEMON_URL, OD_API_TOKEN,
    // OD_AUTH_MODE, OD_BASIC_USER, OD_BASIC_PASS (validadas en el daemon).
    property var catalog: [
        // Entry point REAL de oraios/serena: el script es `serena` con el
        // subcommand `start-mcp-server` (pyproject [project.scripts] verificado
        // 2026-06-10; `serena-mcp-server` no existe → Connection closed).
        { id: "serena",      name: "Serena",      tag: "Código",  desc: "Edición semántica de código y navegación por símbolos.",
          argv: ["uvx", "--from", "git+https://github.com/oraios/serena", "serena", "start-mcp-server"] },
        { id: "github",      name: "GitHub",      tag: "Dev",     desc: "MCP oficial de GitHub: repos, issues, PRs, código.",
          argv: ["npx", "-y", "@modelcontextprotocol/server-github"] },
        { id: "context7",    name: "Context7",    tag: "Docs",    desc: "Documentación de librerías en vivo, siempre actualizada.",
          argv: ["npx", "-y", "@upstash/context7-mcp"] },
        // open-design: paquete PUBLICADO en npm → npx lo descarga y ejecuta.
        // Requiere BYOK (envSchema): el usuario aporta la URL de su daemon Open
        // Design y opcionalmente un token de autenticación.
        { id: "open-design", name: "Open Design", tag: "Diseño",  desc: "Puente MCP a Open Design (BYOK) — diseño desde el agente.",
          argv: ["npx", "-y", "open-design-mcp"],
          envSchema: [
              { key: "OD_DAEMON_URL",  label: "URL del daemon Open Design", required: true,  secret: false, kind: "url",
                hint: "Ej.: http://localhost:3000" },
              { key: "OD_API_TOKEN",   label: "Token de API (opcional)",     required: false, secret: true,  kind: "text",
                hint: "Déjalo vacío si no usas autenticación con token" },
              { key: "OD_AUTH_MODE",   label: "Modo de autenticación",       required: false, secret: false, kind: "text",
                hint: "bearer | basic — vacío si no hay auth" },
              { key: "OD_BASIC_USER",  label: "Usuario HTTP-Basic",          required: false, secret: false, kind: "text",
                hint: "Solo si OD_AUTH_MODE=basic" },
              { key: "OD_BASIC_PASS",  label: "Contraseña HTTP-Basic",       required: false, secret: true,  kind: "text",
                hint: "Solo si OD_AUTH_MODE=basic" }
          ]
        },
        // "mcp-libreoffice" NO existe en PyPI (404, verificado 2026-06-10) —
        // era un paquete fantasma que garantizaba "Connection closed". Fuera
        // del catálogo hasta tener un MCP de oficina real (regla: nunca
        // maquillar estado). Los documentos se manejan via Filesystem + las
        // apps LibreOffice nativas.
        { id: "filesystem",  name: "Filesystem",  tag: "Sistema", desc: "Lectura, escritura y navegación de ficheros locales con HITL.",
          argv: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/var/home/hermes-user"] },
        { id: "sqlite",      name: "SQLite",      tag: "Datos",   desc: "Consultas a bases de datos SQLite locales.",
          argv: ["uvx", "mcp-server-sqlite", "--db-path", "/var/home/hermes-user/.local/share/hermes/sqlite.db"] }
    ]

    property var servers: []          // configurados (daemon)
    property bool loading: true
    property string note: ""
    property string busyId: ""        // server_id en instalación

    // ── BYOK config dialog state ─────────────────────────────────────────
    property bool byokOpen: false
    property var  byokCatalogEntry: null    // entry del catálogo que se está configurando
    property var  byokValues: ({})          // {key: value} acumulado desde los inputs

    // ── Registry oficial MCP (registry.modelcontextprotocol.io) ─────────
    // El daemon consume el registry (verbo search_mcp_registry) y devuelve
    // entries NORMALIZADAS con argv listo (npm→npx / pypi→uvx). El SO no
    // inventa catálogo: el curado es el arranque, el registry es el estándar.
    property string source: "curado"        // curado | registry
    property string registryQuery: ""
    property var registryResults: []
    property bool registryLoading: false

    function searchRegistry(q) {
        if (!q || q.trim().length < 2) { registryResults = []; return; }
        registryLoading = true;
        hermes.call("mcpreg-search", "search_mcp_registry",
                    JSON.stringify({ query: q.trim(), limit: 30 }));
    }
    // name reverse-DNS ("io.github.owner/repo") → ServerSlug [a-z0-9-]
    function slugify(name) {
        var s = (name || "").toLowerCase().replace(/[^a-z0-9]+/g, "-");
        return s.replace(/^-+|-+$/g, "").substring(0, 60) || "mcp-server";
    }
    function activeModel() {
        if (source === "curado") return catalog;
        var out = [];
        for (var i = 0; i < registryResults.length; i++) {
            var r = registryResults[i];
            out.push({
                id: slugify(r.name), name: r.name,
                tag: r.installable ? (r.runner || "mcp") : "no soportado",
                desc: (r.description || "") + (r.installable ? "" : (" — " + (r.unsupported_reason || "solo OCI/remote, próximamente"))),
                argv: r.argv || [], installable: r.installable === true
            });
        }
        return out;
    }
    Timer { id: regDebounce; interval: 450; onTriggered: searchRegistry(app.registryQuery) }

    function load() { hermes.call("mcp-list", "list_mcp_servers", "{}"); }
    function installed(cid) {
        for (var i = 0; i < servers.length; i++) if (servers[i].server_id === cid) return servers[i];
        return null;
    }
    function install(c) {
        if (busyId.length > 0) return;
        // Servers with envSchema require BYOK config before the security scan.
        if (c.envSchema && c.envSchema.length > 0) {
            byokCatalogEntry = c;
            byokValues = ({});
            byokOpen = true;
            return;
        }
        _launchInstall(c, {});
    }

    // Called after BYOK dialog is confirmed (or directly for entries without envSchema).
    function _launchInstall(c, envOverrides) {
        busyId = c.id;
        note = "Centro de Seguridad: analizando " + c.name + "…";
        var draft = { server_id: c.id, label: c.name, argv: c.argv };
        // Only include env key in draft when non-empty; daemon validates the schema.
        var envKeys = Object.keys(envOverrides);
        if (envKeys.length > 0) draft["env"] = envOverrides;
        // Gate: el Centro de Seguridad escanea ANTES de instalar y muestra el
        // score; sólo si el usuario confirma se conecta el servidor (add_mcp_server).
        root.beginGatedInstall(
            { kind: "mcp_server", identifier: c.id, argv: c.argv },
            "add_mcp_server",
            "mcp-add-" + c.id,
            { draft_json: draft }
        );
    }

    // El gate terminó SIN instalar (cancelar/bloqueado/error) → liberar busy.
    Connections {
        target: root
        function onInstallResolved(reqId) {
            if (reqId.indexOf("mcp-add-") === 0) { app.busyId = ""; app.note = ""; }
        }
    }
    function remove(sid) { hermes.call("mcp-del", "remove_mcp_server", JSON.stringify({ server_id: sid })); }

    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "mcpreg-search") {
                app.registryLoading = false;
                try { app.registryResults = ok ? JSON.parse(jsonStr || "[]") : []; }
                catch (e) { app.registryResults = []; }
                if (app.registryResults.length === 0 && app.registryQuery.trim().length >= 2)
                    app.note = "Sin resultados en el Registry para \"" + app.registryQuery + "\"";
                else app.note = "";
            } else if (reqId === "mcp-list") {
                app.loading = false;
                try { app.servers = ok ? JSON.parse(jsonStr || "[]") : []; } catch (e) { app.servers = []; }
            } else if (reqId.indexOf("mcp-add-") === 0) {
                app.busyId = "";
                try {
                    var r = JSON.parse(jsonStr || "{}");
                    if (ok && r.ok) { app.note = "✓ Conectado (" + (r.tool_count || 0) + " tools disponibles para Hermes)"; root.showToast("MCP conectado", "success"); }
                    else { app.note = "✕ " + (r.error || jsonStr); }
                } catch (e) { app.note = ok ? "✓" : ("✕ " + jsonStr); }
                app.load();
            } else if (reqId === "mcp-del") { app.note = ""; app.load(); }
        }
    }
    Component.onCompleted: load()
    // La salud cambia sola (reconexión al boot, caídas) → refresco suave.
    Timer { interval: 15000; running: true; repeat: true; onTriggered: app.load() }

    Column {
        anchors.fill: parent
        anchors.margins: Math.round(Tokens.spXl * sf)
        spacing: Math.round(Tokens.spMd * sf)

        // ── Header ──
        Column {
            width: parent.width
            spacing: Math.round(Tokens.spXs * sf)
            Text {
                text: "MCP Apps"
                color: Tokens.textPrimary
                font.pixelSize: Math.round(18 * sf)
                font.family: Tokens.fontDisplay
                font.weight: Font.DemiBold
            }
            Text {
                text: "Servidores MCP que extienden a Hermes con tools nuevas. Catálogo curado + el Registry oficial (registry.modelcontextprotocol.io). Cada tool pasa por el Centro de Seguridad (HITL)."
                color: Tokens.textMuted
                font.pixelSize: Math.round(12 * sf)
                font.family: Tokens.fontBody
                width: parent.width
                wrapMode: Text.WordWrap
            }
        }

        // ── Selector de fuente: Curado | Registry oficial ──
        Row {
            spacing: Math.round(Tokens.spXs * sf)
            Repeater {
                model: [{ id: "curado", label: "Curado" }, { id: "registry", label: "Registry oficial" }]
                Rectangle {
                    height: Math.round(30 * sf)
                    width: srcLbl.width + Math.round(Tokens.spXl * sf)
                    radius: Math.round(Tokens.radiusSm * sf)
                    color: app.source === modelData.id ? Tokens.accentSubtle : Tokens.bgElevated
                    border.width: 1
                    border.color: app.source === modelData.id ? Tokens.accentBase : Tokens.borderDefault
                    Text {
                        id: srcLbl
                        anchors.centerIn: parent
                        text: modelData.label
                        color: app.source === modelData.id ? Tokens.accentBase : Tokens.textSecondary
                        font.pixelSize: Math.round(11 * sf)
                        font.family: Tokens.fontBody
                        font.weight: app.source === modelData.id ? Font.DemiBold : Font.Normal
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: { app.source = modelData.id; app.note = ""; }
                    }
                }
            }
        }

        // ── Buscador del Registry oficial ──
        LumenInput {
            visible: app.source === "registry"
            sf: app.sf
            width: parent.width
            placeholder: "Busca en el Registry oficial: github, slack, postgres, playwright…"
            onAccepted: { app.registryQuery = text; regDebounce.restart(); }
            // Debounce on every keystroke via text binding
            onTextChanged: { app.registryQuery = text; regDebounce.restart(); }
        }

        // ── Note / status line ──
        Text {
            visible: app.note.length > 0
            text: app.note
            color: app.note.indexOf("✕") === 0 ? Tokens.dangerBase
                 : app.note.indexOf("✓") === 0 ? Tokens.successBase
                 : Tokens.textSecondary
            font.pixelSize: Math.round(12 * sf)
            font.family: Tokens.fontBody
            width: parent.width
            wrapMode: Text.WordWrap
        }

        // ── Server list ──
        ListView {
            id: mcpList
            width: parent.width
            height: app.height - y - Math.round(Tokens.spXl * sf)
            spacing: Math.round(Tokens.spSm * sf)
            clip: true
            model: app.activeModel()
            ScrollBar.vertical: LumenScrollBar { sf: app.sf; policy: ScrollBar.AsNeeded }

            WheelHandler {
                acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                onWheel: (event) => {
                    var f = mcpList;
                    f.contentY = Math.max(0, Math.min(Math.max(0, f.contentHeight - f.height), f.contentY - event.angleDelta.y));
                }
            }

            delegate: Rectangle {
                width: ListView.view.width
                height: cRow.implicitHeight + Math.round(Tokens.spXl * sf)
                radius: Math.round(Tokens.radiusMd * sf)
                color: Tokens.bgCard
                border.width: 1
                border.color: Tokens.borderSubtle
                property var inst: app.installed(modelData.id)

                Row {
                    id: cRow
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.leftMargin: Math.round(Tokens.spMd * sf)
                    anchors.rightMargin: Math.round(Tokens.spMd * sf)
                    spacing: Math.round(Tokens.spMd * sf)

                    // Icon avatar
                    Rectangle {
                        width: Math.round(34 * sf)
                        height: Math.round(34 * sf)
                        radius: Math.round(Tokens.radiusSm * sf)
                        color: Tokens.accentGhost
                        anchors.verticalCenter: parent.verticalCenter
                        Text {
                            anchors.centerIn: parent
                            text: modelData.name.charAt(0).toUpperCase()
                            color: Tokens.accentBase
                            font.pixelSize: Math.round(15 * sf)
                            font.family: Tokens.fontDisplay
                            font.weight: Font.Bold
                        }
                    }

                    // Name + description
                    Column {
                        width: parent.width - Math.round(34 * sf) - actBtn.width - Math.round(36 * sf)
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Math.round(Tokens.spXs * sf)

                        Row {
                            spacing: Math.round(Tokens.spSm * sf)
                            Text {
                                text: modelData.name
                                color: Tokens.textPrimary
                                font.pixelSize: Math.round(14 * sf)
                                font.family: Tokens.fontDisplay
                                font.weight: Font.DemiBold
                            }
                            LumenChip {
                                sf: app.sf
                                text: modelData.tag
                                tone: "neutral"
                                anchors.verticalCenter: parent.verticalCenter
                            }
                            LumenChip {
                                visible: inst !== null
                                sf: app.sf
                                text: inst ? (inst.health === "healthy" ? "● " + inst.tool_count + " tools" : "● " + inst.health) : ""
                                tone: inst && inst.health === "healthy" ? "success" : "danger"
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }
                        Text {
                            text: modelData.desc
                            color: Tokens.textMuted
                            font.pixelSize: Math.round(11 * sf)
                            font.family: Tokens.fontBody
                            width: parent.width
                            wrapMode: Text.WordWrap
                            maximumLineCount: 2
                            elide: Text.ElideRight
                        }
                    }

                    // Action button
                    LumenButton {
                        id: actBtn
                        sf: app.sf
                        // installable === false: entry del Registry solo OCI/remote
                        // — botón deshabilitado honesto (no fingimos instalar).
                        property bool canInstall: modelData.installable === undefined || modelData.installable === true
                        label: app.busyId === modelData.id ? "…"
                             : inst !== null ? "Quitar"
                             : canInstall ? "Instalar"
                             : "No disponible"
                        variant: inst !== null ? "secondary" : canInstall ? "primary" : "ghost"
                        enabled: (canInstall || inst !== null) && app.busyId !== modelData.id
                        opacity: canInstall || inst !== null ? 1.0 : 0.55
                        implicitWidth: Math.round(96 * sf)
                        implicitHeight: Math.round(32 * sf)
                        anchors.verticalCenter: parent.verticalCenter
                        onClicked: inst !== null ? app.remove(modelData.id) : app.install(modelData)
                    }
                }
            }
        }
    }

    // ── BYOK configuration modal ─────────────────────────────────────────────
    // Declared LAST so it sits above all sibling content in paint order.
    // LumenModal also carries z: Tokens.zModal for belt-and-suspenders safety.
    // Shown when a catalog entry declares envSchema. Collects the required BYOK
    // values and passes them as `env` in the add_mcp_server draft.
    // Security: values are NOT logged; secret fields use password echo mode.
    LumenModal {
        id: byokModal
        sf: app.sf
        open: app.byokOpen
        onClosed: { app.byokOpen = false; app.byokCatalogEntry = null; }

        Column {
            width: parent.width
            spacing: Math.round(Tokens.spMd * app.sf)

            // ── Title ──
            Text {
                text: app.byokCatalogEntry ? "Configurar " + app.byokCatalogEntry.name : ""
                color: Tokens.textPrimary
                font.pixelSize: Math.round(16 * app.sf)
                font.family: Tokens.fontDisplay
                font.weight: Font.DemiBold
                width: parent.width
                wrapMode: Text.WordWrap
            }
            Text {
                text: "Este servidor requiere tus credenciales BYOK. Se guardan cifradas en el daemon y nunca salen del dispositivo."
                color: Tokens.textMuted
                font.pixelSize: Math.round(12 * app.sf)
                font.family: Tokens.fontBody
                width: parent.width
                wrapMode: Text.WordWrap
            }

            // ── One input per schema field ──
            Repeater {
                id: byokRepeater
                model: app.byokCatalogEntry ? app.byokCatalogEntry.envSchema : []
                Column {
                    width: parent.width
                    spacing: Math.round(Tokens.spXs * app.sf)

                    Row {
                        spacing: Math.round(4 * app.sf)
                        Text {
                            text: modelData.label
                            color: Tokens.textSecondary
                            font.pixelSize: Math.round(12 * app.sf)
                            font.family: Tokens.fontBody
                            font.weight: Font.DemiBold
                        }
                        Text {
                            visible: modelData.required === true
                            text: "*"
                            color: Tokens.dangerBase
                            font.pixelSize: Math.round(12 * app.sf)
                            font.family: Tokens.fontBody
                        }
                    }
                    LumenInput {
                        sf: app.sf
                        width: parent.width
                        placeholder: modelData.hint || ""
                        password: modelData.secret === true
                        // Store value on every keystroke so Confirm can harvest all fields.
                        onTextChanged: {
                            var updated = app.byokValues;
                            if (text.length > 0) updated[modelData.key] = text;
                            else delete updated[modelData.key];
                            app.byokValues = updated;
                        }
                    }
                }
            }

            // ── Buttons ──
            Row {
                spacing: Math.round(Tokens.spSm * app.sf)
                layoutDirection: Qt.RightToLeft
                width: parent.width

                LumenButton {
                    sf: app.sf
                    label: "Instalar"
                    variant: "primary"
                    implicitWidth: Math.round(100 * app.sf)
                    implicitHeight: Math.round(36 * app.sf)
                    enabled: {
                        if (!app.byokCatalogEntry) return false;
                        var schema = app.byokCatalogEntry.envSchema;
                        for (var i = 0; i < schema.length; i++) {
                            if (schema[i].required && !app.byokValues[schema[i].key])
                                return false;
                        }
                        return true;
                    }
                    onClicked: {
                        var entry = app.byokCatalogEntry;
                        var collected = app.byokValues;
                        app.byokOpen = false;
                        app.byokCatalogEntry = null;
                        app._launchInstall(entry, collected);
                    }
                }
                LumenButton {
                    sf: app.sf
                    label: "Cancelar"
                    variant: "secondary"
                    implicitWidth: Math.round(100 * app.sf)
                    implicitHeight: Math.round(36 * app.sf)
                    onClicked: byokModal.closed()
                }
            }
        }
    }
}
