import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// ── ProvidersApp ──────────────────────────────────────────────────────────
// Cableado REAL al daemon (D-Bus): ListProviders/AddProvider/TestProvider/
// SetActiveProvider/DeleteProvider. Sin mocks. La key viaja por el bus al
// SecretsVault del daemon; nunca a HTTP/log.
Rectangle {
    id: app
    anchors.fill: parent
    color: Tokens.bgSurface
    radius: Math.round(Tokens.radiusLg * sf)
    // Responsive: action buttons always get their space; info column fills the rest
    readonly property bool _compact: app.width < Tokens.bpCompact * root.sf

    property var providers: []
    property bool loading: true
    property bool adding: false
    property bool busy: false
    property string note: ""
    readonly property real sf: root.sf

    property var aliasField: null
    property var keyField: null
    property var modelField: null
    property var urlField: null

    property var kinds: [
        { id: "openai",            label: "OpenAI",            model: "gpt-5.4-nano",            url: "https://api.openai.com/v1", key: true },
        { id: "anthropic",         label: "Anthropic",         model: "claude-sonnet-4-6",       url: "",                          key: true },
        { id: "gemini",            label: "Gemini",            model: "gemini-2.0-flash",        url: "",                          key: true },
        { id: "deepseek",          label: "DeepSeek",          model: "deepseek-chat",           url: "",                          key: true },
        { id: "groq",              label: "Groq",              model: "llama-3.3-70b-versatile", url: "https://api.groq.com/openai/v1", key: true },
        { id: "ollama",            label: "Ollama (local)",    model: "llama3.1",                url: "http://localhost:11434",    key: false },
        { id: "openai_compatible", label: "Compatible OpenAI", model: "",                        url: "http://localhost:8080/v1",  key: true }
    ]
    property int sel: 0
    property var k: kinds[sel]

    property var nativeCatalog: []
    property bool showCatalog: false

    property var activeProv: ({})
    function load() {
        loading = true;
        hermes.call("prov-list", "list_providers", "{}");
        hermes.call("prov-native", "list_native_providers", "{}");
        hermes.call("prov-active", "get_active_provider", "{}");
    }

    // ── OAuth device-code (suscripciones: Nous Portal) ──
    property string oauthState: "idle"
    property string oauthUserCode: ""
    property string oauthUrl: ""
    property string oauthSession: ""
    property string oauthError: ""
    Timer {
        id: oauthPollTimer
        interval: 3000; repeat: true; running: false
        onTriggered: hermes.call("prov-oauth-status", "get_provider_oauth_status",
                                 JSON.stringify({ session_id: app.oauthSession }))
    }
    property string oauthProvider: ""
    function startOauth(p) {
        oauthState = "starting"; oauthError = ""; oauthUserCode = ""; oauthUrl = ""; note = "";
        oauthProvider = p.id;
        hermes.call("prov-oauth-start", "start_provider_oauth", JSON.stringify({ provider_id: p.id }));
    }
    function cancelOauth() { oauthPollTimer.stop(); oauthState = "idle"; oauthSession = ""; }

    // ── Configuración NATIVA (api-key) ──
    property bool nativeConfig: false
    property string nativePid: ""
    property string nativeName: ""
    property var nativeKeyField: null
    property var nativeModelField: null
    readonly property var defaultModels: ({
        "openai-api": "gpt-5.4-nano", "anthropic-api": "claude-sonnet-4-6",
        "gemini": "gemini-2.0-flash", "deepseek": "deepseek-chat",
        "groq": "llama-3.3-70b-versatile", "mistral": "mistral-large-latest",
        "openrouter": "openai/gpt-5.4-nano", "copilot": "gpt-4o",
        "moonshot": "kimi-k2", "xai": "grok-4", "together": "", "cerebras": ""
    })

    function pickNative(p) {
        showCatalog = false; note = "";
        if (p.auth_type === "api_key") {
            nativeConfig = true; adding = false;
            nativePid = p.id; nativeName = p.name;
            if (nativeKeyField) nativeKeyField.text = "";
            if (nativeModelField) nativeModelField.text = defaultModels[p.id] || "";
            return;
        }
        if (p.id === "nous" || p.id === "openai-codex" || p.id === "xai-oauth") { startOauth(p); return; }
        note = p.name + " usa " + p.auth_type + " (OAuth navegador) — Nous, OpenAI Codex y xAI ya funcionan; Gemini/Qwen en breve.";
    }
    function configureNative() {
        if (busy || !nativeKeyField || nativeKeyField.text.trim().length === 0) { note = "Pega la API key"; return; }
        busy = true; note = "Configurando " + nativeName + "…";
        hermes.call("prov-native-cfg", "configure_native_provider", JSON.stringify({ draft_json: {
            provider_id: nativePid,
            api_key: nativeKeyField.text.trim(),
            model: nativeModelField ? nativeModelField.text.trim() : "",
            base_url: ""
        }}));
    }
    function add() {
        if (busy) return;
        note = ""; busy = true;
        hermes.call("prov-add", "add_provider", JSON.stringify({ draft_json: {
            kind: k.id, alias: (aliasField ? aliasField.text.trim() : "") || k.label,
            default_model: modelField ? modelField.text.trim() : k.model,
            base_url: urlField ? urlField.text.trim() : k.url,
            api_key: keyField ? keyField.text : "", set_active: providers.length === 0
        }}));
    }
    function activate(pid) { hermes.call("prov-activate", "set_active_provider", JSON.stringify({ provider_id: pid })); }
    function test(pid) { note = "Probando…"; hermes.call("prov-test-" + pid, "test_provider", JSON.stringify({ provider_id: pid })); }
    function del(pid) { hermes.call("prov-del", "delete_provider", JSON.stringify({ provider_id: pid })); }

    Connections {
        target: hermes
        function onResult(reqId, ok, jsonStr) {
            if (reqId === "prov-list") {
                app.loading = false;
                try { app.providers = ok ? JSON.parse(jsonStr || "[]") : []; } catch (e) { app.providers = []; }
            } else if (reqId === "prov-native") {
                try { app.nativeCatalog = ok ? JSON.parse(jsonStr || "[]") : []; } catch (e) { app.nativeCatalog = []; }
            } else if (reqId === "prov-active") {
                try { app.activeProv = ok ? JSON.parse(jsonStr || "{}") : ({}); } catch (e) { app.activeProv = ({}); }
            } else if (reqId === "prov-add") {
                app.busy = false;
                if (ok) { app.adding = false; if (app.keyField) app.keyField.text = ""; app.note = ""; app.load(); root.showToast("Proveedor añadido", "success"); }
                else { try { app.note = "✕ " + (JSON.parse(jsonStr).error || jsonStr); } catch (e) { app.note = "✕ " + jsonStr; } }
            } else if (reqId === "prov-native-cfg") {
                app.busy = false;
                var rc = {}; try { rc = JSON.parse(jsonStr || "{}"); } catch (e) {}
                if (ok && rc.ok) {
                    app.nativeConfig = false; if (app.nativeKeyField) app.nativeKeyField.text = "";
                    app.note = ""; app.load();
                    root.showToast(app.nativeName + " configurado — ya es el cerebro activo", "success");
                } else {
                    app.note = "✕ " + (rc.error || jsonStr);
                }
            } else if (reqId === "prov-activate") { app.load(); root.showToast("Proveedor activado", "success"); }
            else if (reqId === "prov-del") { app.load(); }
            else if (reqId.indexOf("prov-test-") === 0) {
                try { var r = JSON.parse(jsonStr || "{}"); app.note = r.ok ? "✓ Conexión correcta" : ("✕ " + (r.error || "falló")); }
                catch (e) { app.note = ok ? "✓" : "✕"; }
                app.load();
            }
            else if (reqId === "prov-oauth-start") {
                try {
                    var o = JSON.parse(jsonStr || "{}");
                    if (!ok || o.error) { app.oauthState = "error"; app.oauthError = o.error || jsonStr; return; }
                    app.oauthSession = o.session_id || "";
                    app.oauthState = "pending";
                    if (o.flow === "loopback" && o.auth_url) {
                        app.oauthUserCode = "";
                        app.oauthUrl = o.auth_url;
                        root.launchNative("chromium-browser --ozone-platform=wayland --no-sandbox --password-store=basic " + o.auth_url, "chromium");
                    } else {
                        app.oauthUserCode = o.user_code || "";
                        app.oauthUrl = o.verification_url || "";
                    }
                    oauthPollTimer.start();
                } catch (e) { app.oauthState = "error"; app.oauthError = String(e); }
            }
            else if (reqId === "prov-oauth-status") {
                try {
                    var st = JSON.parse(jsonStr || "{}");
                    if (st.status === "approved") {
                        oauthPollTimer.stop();
                        app.oauthState = "approved";
                        app.load();
                        root.showToast((app.oauthProvider === "openai-codex" ? "OpenAI Codex" : (app.oauthProvider === "xai-oauth" ? "xAI" : "Nous Portal")) + " conectado", "success");
                    } else if (st.status === "error") {
                        oauthPollTimer.stop();
                        app.oauthState = "error";
                        app.oauthError = st.error_message || "falló la autorización";
                    } else if (st.status === "unknown") {
                        oauthPollTimer.stop();
                        app.oauthState = "error";
                        app.oauthError = "sesión expirada — reinicia el flujo";
                    }
                } catch (e) { /* sigue sondeando */ }
            }
        }
    }
    Component.onCompleted: load()

    // ── Layout ────────────────────────────────────────────────────────────
    Column {
        anchors.fill: parent
        anchors.margins: Math.round(Tokens.spXl * sf)
        spacing: Math.round(Tokens.spLg * sf)

        // ── Header ──
        Row {
            width: parent.width

            Column {
                width: parent.width - addProvBtn.width - Math.round(Tokens.spMd * sf)
                spacing: Math.round(Tokens.spXs * sf)

                Text {
                    text: "Proveedores de IA"
                    color: Tokens.textPrimary
                    font.family: Tokens.fontDisplay
                    font.pixelSize: Math.round(18 * sf)
                    font.weight: Font.DemiBold
                }
                Text {
                    text: "El cerebro de Hermes. El proveedor activo es el que piensa."
                    color: Tokens.textMuted
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(12 * sf)
                }
            }

            LumenButton {
                id: addProvBtn
                sf: app.sf
                label: app.adding ? "Cancelar" : "+ Añadir"
                variant: app.adding ? "secondary" : "primary"
                implicitWidth: Math.round(120 * sf)
                anchors.verticalCenter: parent.verticalCenter
                onClicked: { app.adding = !app.adding; app.note = ""; }
            }
        }

        // ── Catálogo NATIVO de Hermes (toggler) ──
        Rectangle {
            width: parent.width
            height: Math.round(34 * sf)
            radius: Math.round(Tokens.radiusSm * sf)
            color: Tokens.bgElevated
            border.width: 1
            border.color: Tokens.borderDefault
            visible: app.nativeCatalog.length > 0

            Row {
                anchors.fill: parent
                anchors.leftMargin: Math.round(Tokens.spMd * sf)
                anchors.rightMargin: Math.round(Tokens.spMd * sf)
                spacing: Math.round(Tokens.spSm * sf)

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: app.showCatalog ? "▾" : "▸"
                    color: Tokens.textMuted
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                }
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Catálogo nativo de Hermes (" + app.nativeCatalog.length + " providers — incluye suscripciones OAuth)"
                    color: Tokens.textSecondary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                }
            }
            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: app.showCatalog = !app.showCatalog
            }
        }

        // ── Catálogo NATIVO expandido ──
        Rectangle {
            width: parent.width
            visible: app.showCatalog
            height: visible ? Math.min(Math.round(260 * sf), catList.contentHeight + Math.round(Tokens.spLg * sf)) : 0
            radius: Math.round(Tokens.radiusMd * sf)
            color: Tokens.bgCard
            border.width: 1
            border.color: Tokens.borderSubtle

            ListView {
                id: catList
                anchors.fill: parent
                anchors.margins: Math.round(Tokens.spSm * sf)
                clip: true
                spacing: Math.round(Tokens.spXs * sf)
                model: app.nativeCatalog

                ScrollBar.vertical: LumenScrollBar { sf: app.sf }

                WheelHandler {
                    acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                    onWheel: (event) => {
                        catList.contentY = Math.max(0, Math.min(
                            Math.max(0, catList.contentHeight - catList.height),
                            catList.contentY - event.angleDelta.y));
                    }
                }

                delegate: Rectangle {
                    width: catList.width
                    height: Math.round(36 * sf)
                    radius: Math.round(Tokens.radiusSm * sf)
                    color: nMa.containsMouse ? Tokens.bgElevated : "transparent"

                    Behavior on color {
                        enabled: !Tokens.reduceMotion
                        ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                    }

                    Row {
                        anchors.fill: parent
                        anchors.leftMargin: Math.round(Tokens.spMd * sf)
                        anchors.rightMargin: Math.round(Tokens.spMd * sf)
                        spacing: Math.round(Tokens.spSm * sf)

                        Text {
                            text: modelData.name
                            color: Tokens.textPrimary
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(12 * sf)
                            anchors.verticalCenter: parent.verticalCenter
                            width: parent.width - Math.round(150 * sf)
                            elide: Text.ElideRight
                        }

                        LumenChip {
                            sf: app.sf
                            anchors.verticalCenter: parent.verticalCenter
                            text: modelData.auth_type === "api_key" ? "API key" : "OAuth / suscripción"
                            tone: modelData.auth_type === "api_key" ? "success" : "warn"
                        }
                    }

                    MouseArea {
                        id: nMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: app.pickNative(modelData)
                    }
                }
            }
        }

        // ── Tarjeta flow OAuth ──
        Rectangle {
            width: parent.width
            visible: app.oauthState !== "idle"
            height: visible ? oauthCol.implicitHeight + Math.round(Tokens.spXl * sf) : 0
            radius: Math.round(Tokens.radiusMd * sf)
            color: Tokens.bgCard
            border.width: 1
            border.color: {
                if (app.oauthState === "error")    return Tokens.dangerBase
                if (app.oauthState === "approved") return Tokens.successBase
                return Tokens.warnBase
            }

            Column {
                id: oauthCol
                anchors.fill: parent
                anchors.margins: Math.round(Tokens.spLg * sf)
                spacing: Math.round(Tokens.spSm * sf)

                Row {
                    width: parent.width
                    spacing: Math.round(Tokens.spSm * sf)

                    Text {
                        property string pn: app.oauthProvider === "openai-codex" ? "OpenAI Codex"
                                          : app.oauthProvider === "xai-oauth" ? "xAI (SuperGrok)"
                                          : "Nous Portal"
                        text: app.oauthState === "starting"  ? ("Iniciando conexión con " + pn + "…")
                            : app.oauthState === "pending"   ? ("Autoriza este equipo en " + pn)
                            : app.oauthState === "approved"  ? ("✓ Suscripción " + pn + " conectada")
                            : "✕ No se pudo conectar"
                        color: app.oauthState === "error"    ? Tokens.dangerBase
                             : app.oauthState === "approved" ? Tokens.successBase
                             : Tokens.textPrimary
                        font.family: Tokens.fontDisplay
                        font.pixelSize: Math.round(14 * sf)
                        font.weight: Font.DemiBold
                        width: parent.width - oauthCancelBtn.width - Math.round(Tokens.spSm * sf)
                        wrapMode: Text.WordWrap
                    }

                    LumenButton {
                        id: oauthCancelBtn
                        sf: app.sf
                        label: "Cancelar"
                        variant: "secondary"
                        implicitWidth: Math.round(90 * sf)
                        implicitHeight: Math.round(30 * sf)
                        visible: app.oauthState !== "approved"
                        onClicked: app.cancelOauth()
                    }
                }

                Text {
                    visible: app.oauthState === "pending" && app.oauthUserCode.length === 0
                    text: "Se ha abierto el navegador. Inicia sesión y autoriza el acceso; al volver, esto se conecta solo."
                    color: Tokens.textSecondary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(12 * sf)
                    width: parent.width
                    wrapMode: Text.WordWrap
                }

                Text {
                    visible: app.oauthState === "pending" && app.oauthUserCode.length > 0
                    text: "1. Abre esta dirección en cualquier dispositivo:"
                    color: Tokens.textSecondary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(12 * sf)
                }

                // URL copiable
                Rectangle {
                    visible: app.oauthState === "pending" && app.oauthUserCode.length > 0 && app.oauthUrl.length > 0
                    width: parent.width
                    height: Math.round(34 * sf)
                    radius: Math.round(Tokens.radiusSm * sf)
                    color: Tokens.bgElevated
                    border.width: 1
                    border.color: Tokens.borderDefault

                    TextInput {
                        anchors.fill: parent
                        anchors.leftMargin: Math.round(Tokens.spMd * sf)
                        anchors.rightMargin: Math.round(Tokens.spMd * sf)
                        verticalAlignment: TextInput.AlignVCenter
                        text: app.oauthUrl
                        readOnly: true
                        selectByMouse: true
                        color: Tokens.accentBase
                        font.family: Tokens.fontMono
                        font.pixelSize: Math.round(12 * sf)
                    }
                }

                Text {
                    visible: app.oauthState === "pending" && app.oauthUserCode.length > 0
                    text: "2. Introduce este código cuando te lo pida:"
                    color: Tokens.textSecondary
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(12 * sf)
                }

                // Device code display
                Text {
                    visible: app.oauthState === "pending" && app.oauthUserCode.length > 0
                    text: app.oauthUserCode
                    color: Tokens.textPrimary
                    font.family: Tokens.fontMono
                    font.pixelSize: Math.round(26 * sf)
                    font.weight: Font.Bold
                    font.letterSpacing: 3
                }

                Text {
                    visible: app.oauthState === "pending"
                    text: "Esperando autorización… (esta tarjeta se actualiza sola)"
                    color: Tokens.textMuted
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                }

                Text {
                    visible: app.oauthState === "error" && app.oauthError.length > 0
                    text: app.oauthError
                    color: Tokens.dangerBase
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                    width: parent.width
                    wrapMode: Text.WordWrap
                }
            }
        }

        // ── Form NATIVO (api-key) ──
        Rectangle {
            width: parent.width
            visible: app.nativeConfig
            height: visible ? nCol.implicitHeight + Math.round(Tokens.spXl * sf) : 0
            radius: Math.round(Tokens.radiusMd * sf)
            color: Tokens.bgCard
            border.width: 1
            border.color: Tokens.accentBase

            Column {
                id: nCol
                anchors.fill: parent
                anchors.margins: Math.round(Tokens.spLg * sf)
                spacing: Math.round(Tokens.spMd * sf)

                Row {
                    width: parent.width

                    Text {
                        text: "Configurar " + app.nativeName
                        color: Tokens.textPrimary
                        font.family: Tokens.fontDisplay
                        font.pixelSize: Math.round(14 * sf)
                        font.weight: Font.DemiBold
                        width: parent.width - nCancel.width - Math.round(Tokens.spSm * sf)
                        elide: Text.ElideRight
                    }

                    LumenButton {
                        id: nCancel
                        sf: app.sf
                        label: "Cancelar"
                        variant: "secondary"
                        implicitWidth: Math.round(88 * sf)
                        implicitHeight: Math.round(28 * sf)
                        onClicked: { app.nativeConfig = false; app.note = ""; }
                    }
                }

                Text {
                    text: "Provider nativo de Hermes (" + app.nativePid + "). La clave va a ~/.hermes/.env; el modelo a config.yaml. El motor lo usa directo."
                    color: Tokens.textMuted
                    font.family: Tokens.fontBody
                    font.pixelSize: Math.round(11 * sf)
                    width: parent.width
                    wrapMode: Text.WordWrap
                }

                // API key input (password mode — key never shown in clear)
                LumenInput {
                    id: nKeyInp
                    sf: app.sf
                    width: parent.width
                    placeholder: "API key (sk-…, gsk_…, etc.)"
                    password: true
                    Component.onCompleted: app.nativeKeyField = nKeyInp
                }

                // Model input
                LumenInput {
                    id: nModelInp
                    sf: app.sf
                    width: parent.width
                    placeholder: "Modelo (ej. gpt-5.4-nano)"
                    Component.onCompleted: app.nativeModelField = nModelInp
                }

                LumenButton {
                    sf: app.sf
                    label: app.busy ? "…" : "Configurar"
                    variant: "primary"
                    loading: app.busy
                    implicitWidth: Math.round(120 * sf)
                    anchors.right: parent.right
                    onClicked: app.configureNative()
                }
            }
        }

        // ── Form añadir proveedor ──
        Rectangle {
            width: parent.width
            visible: app.adding
            height: visible ? formCol.implicitHeight + Math.round(Tokens.spXl * sf) : 0
            radius: Math.round(Tokens.radiusMd * sf)
            color: Tokens.bgCard
            border.width: 1
            border.color: Tokens.borderSubtle

            Column {
                id: formCol
                anchors.fill: parent
                anchors.margins: Math.round(Tokens.spLg * sf)
                spacing: Math.round(Tokens.spMd * sf)

                // Kind selector chips
                Flow {
                    width: parent.width
                    spacing: Math.round(Tokens.spXs * sf)

                    Repeater {
                        model: app.kinds
                        Rectangle {
                            height: Math.round(30 * sf)
                            width: kindLabel.width + Math.round(Tokens.spLg * sf)
                            radius: Math.round(Tokens.radiusSm * sf)
                            color: index === app.sel ? Tokens.accentSubtle : Tokens.bgElevated
                            border.width: 1
                            border.color: index === app.sel ? Tokens.accentBase : Tokens.borderDefault

                            Behavior on color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                            }

                            Text {
                                id: kindLabel
                                anchors.centerIn: parent
                                text: modelData.label
                                color: index === app.sel ? Tokens.accentBase : Tokens.textSecondary
                                font.family: Tokens.fontBody
                                font.pixelSize: Math.round(11 * sf)
                            }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    app.sel = index;
                                    if (app.modelField) app.modelField.text = modelData.model;
                                    if (app.urlField)   app.urlField.text   = modelData.url;
                                }
                            }
                        }
                    }
                }

                // Alias field
                LumenInput {
                    id: fAlias
                    sf: app.sf
                    width: parent.width
                    placeholder: "Alias"
                    Component.onCompleted: app.aliasField = fAlias
                }

                // API key field (password — never shown in clear)
                LumenInput {
                    id: fKey
                    sf: app.sf
                    width: parent.width
                    placeholder: "Clave API (sk-…)"
                    password: true
                    visible: app.k.key
                    Component.onCompleted: app.keyField = fKey
                }

                // Model field
                LumenInput {
                    id: fModel
                    sf: app.sf
                    width: parent.width
                    placeholder: "Modelo"
                    Component.onCompleted: { app.modelField = fModel; fModel.text = app.k.model; }
                }

                // Base URL field
                LumenInput {
                    id: fUrl
                    sf: app.sf
                    width: parent.width
                    placeholder: "Base URL (opcional)"
                    Component.onCompleted: { app.urlField = fUrl; fUrl.text = app.k.url; }
                }

                Row {
                    width: parent.width
                    spacing: Math.round(Tokens.spMd * sf)

                    Text {
                        text: app.note
                        color: app.note.indexOf("✓") === 0 ? Tokens.successBase : Tokens.dangerBase
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(11 * sf)
                        anchors.verticalCenter: parent.verticalCenter
                        width: parent.width - saveBtn.width - Math.round(Tokens.spMd * sf)
                        elide: Text.ElideRight
                    }

                    LumenButton {
                        id: saveBtn
                        sf: app.sf
                        label: app.busy ? "Guardando…" : "Conectar"
                        variant: "primary"
                        loading: app.busy
                        implicitWidth: Math.round(110 * sf)
                        onClicked: app.add()
                    }
                }
            }
        }

        // ── Estado vacío / cargando ──
        Text {
            visible: !app.loading && app.providers.length === 0 && !app.adding
            text: "Aún no hay proveedores. Pulsa «+ Añadir» para conectar el cerebro de Hermes."
            color: Tokens.textMuted
            font.family: Tokens.fontBody
            font.pixelSize: Math.round(13 * sf)
            width: parent.width
            wrapMode: Text.WordWrap
        }

        Text {
            visible: app.loading
            text: "Cargando…"
            color: Tokens.textMuted
            font.family: Tokens.fontBody
            font.pixelSize: Math.round(13 * sf)
        }

        // ── Provider ACTIVO (cerebro en uso) ──
        Rectangle {
            width: parent.width
            visible: !!(app.activeProv && app.activeProv.alias)
            height: visible ? Math.round(56 * sf) : 0
            radius: Math.round(Tokens.radiusMd * sf)
            color: Tokens.successSubtle
            border.width: 1
            border.color: Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.35)

            Row {
                anchors.fill: parent
                anchors.leftMargin: Math.round(Tokens.spLg * sf)
                anchors.rightMargin: Math.round(Tokens.spLg * sf)
                spacing: Math.round(Tokens.spMd * sf)

                // Status dot
                Rectangle {
                    width: Math.round(10 * sf)
                    height: width
                    radius: width / 2
                    anchors.verticalCenter: parent.verticalCenter
                    color: Tokens.successBase
                }

                Column {
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Math.round(2 * sf)

                    Text {
                        text: (app.activeProv.alias || "") + "  ·  ACTIVO" + (app.activeProv.native ? "  ·  nativo" : "")
                        color: Tokens.textPrimary
                        font.family: Tokens.fontDisplay
                        font.pixelSize: Math.round(14 * sf)
                        font.weight: Font.DemiBold
                    }
                    Text {
                        text: "Modelo: " + (app.activeProv.default_model || "—")
                        color: Tokens.textMuted
                        font.family: Tokens.fontBody
                        font.pixelSize: Math.round(11 * sf)
                    }
                }
            }
        }

        // ── Lista de proveedores ──
        ListView {
            id: providersList
            width: parent.width
            height: app.height - y - Math.round(Tokens.spXl * sf)
            spacing: Math.round(Tokens.spSm * sf)
            clip: true
            model: app.providers

            ScrollBar.vertical: LumenScrollBar { sf: app.sf }

            WheelHandler {
                acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                onWheel: (event) => {
                    providersList.contentY = Math.max(0, Math.min(
                        Math.max(0, providersList.contentHeight - providersList.height),
                        providersList.contentY - event.angleDelta.y));
                }
            }

            delegate: Rectangle {
                width: ListView.view.width
                height: Math.round(64 * sf)
                radius: Math.round(Tokens.radiusMd * sf)
                color: Tokens.bgCard
                border.width: 1
                border.color: modelData.is_active
                    ? Qt.rgba(Tokens.successBase.r, Tokens.successBase.g, Tokens.successBase.b, 0.40)
                    : Tokens.borderSubtle

                Behavior on border.color {
                    enabled: !Tokens.reduceMotion
                    ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                }

                // Responsive row: dot + info(fill) + actions
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: Math.round(Tokens.spLg * sf)
                    anchors.rightMargin: Math.round(Tokens.spMd * sf)
                    spacing: Math.round(Tokens.spMd * sf)

                    // Connectivity dot
                    Rectangle {
                        Layout.alignment: Qt.AlignVCenter
                        width: Math.round(10 * sf)
                        height: width
                        radius: width / 2
                        color: modelData.connectivity === "reachable"
                            ? Tokens.successBase
                            : (modelData.connectivity === "unreachable" || modelData.connectivity === "unauthorized"
                                ? Tokens.dangerBase
                                : Tokens.textMuted)
                    }

                    // Info column — fills remaining space
                    Column {
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignVCenter
                        spacing: Math.round(2 * sf)

                        Row {
                            spacing: Math.round(Tokens.spSm * sf)

                            Text {
                                text: modelData.alias || modelData.kind
                                color: Tokens.textPrimary
                                font.family: Tokens.fontDisplay
                                font.pixelSize: Math.round(14 * sf)
                                font.weight: Font.DemiBold
                            }

                            LumenChip {
                                visible: modelData.is_active
                                sf: app.sf
                                text: "ACTIVO"
                                tone: "success"
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }

                        Text {
                            text: (modelData.kind || "") + " · " + (modelData.default_model || "sin modelo") + (modelData.has_api_key ? " · 🔑" : "")
                            color: Tokens.textMuted
                            font.family: Tokens.fontBody
                            font.pixelSize: Math.round(11 * sf)
                            elide: Text.ElideRight
                            width: parent.width
                        }
                    }

                    // Action buttons — fixed size, always visible
                    Row {
                        Layout.alignment: Qt.AlignVCenter
                        spacing: Math.round(Tokens.spXs * sf)

                        LumenButton {
                            sf: app.sf
                            label: "Probar"
                            variant: "secondary"
                            implicitWidth: Math.round(68 * sf)
                            implicitHeight: Math.round(30 * sf)
                            onClicked: app.test(modelData.provider_id)
                        }

                        LumenButton {
                            visible: !modelData.is_active
                            sf: app.sf
                            label: "Activar"
                            variant: "primary"
                            implicitWidth: Math.round(68 * sf)
                            implicitHeight: Math.round(30 * sf)
                            onClicked: app.activate(modelData.provider_id)
                        }

                        // Delete button
                        Rectangle {
                            width: Math.round(30 * sf)
                            height: Math.round(30 * sf)
                            radius: Math.round(Tokens.radiusSm * sf)
                            color: delMa.containsMouse ? Tokens.dangerSubtle : "transparent"
                            border.width: 1
                            border.color: delMa.containsMouse ? Tokens.dangerBase : Tokens.borderDefault

                            Behavior on color {
                                enabled: !Tokens.reduceMotion
                                ColorAnimation { duration: Tokens.durFast; easing.type: Easing.OutQuad }
                            }

                            Text {
                                anchors.centerIn: parent
                                text: "🗑"
                                font.pixelSize: Math.round(12 * sf)
                            }

                            MouseArea {
                                id: delMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: app.del(modelData.provider_id)
                            }
                        }
                    }
                }
            }
        }
    }
}
